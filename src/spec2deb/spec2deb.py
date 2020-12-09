#! /usr/bin/env python
"""This utility takes a rpm package.spec as input generating a series of
debian-specific files like the package.dsc build descriptor and the
debian.diff.gz / debian.tar.gz containing the control file and patches.
...........................................................................
The result is a directory that is ready for dpkg-source to build a *.deb.
...........................................................................
Note that the script has some builting "magic" to transform the rpm spec
%build and %install scripts as well. Although it works in a lot of cases
it might be failing in your case. And yes ... we take patches.
"""

import bz2  # @UnresolvedImport
import collections
from functools import partial
import glob
import gzip
import hashlib
import io
import logging
try:
    import lzma
except ImportError:
    from backports import lzma
from optparse import OptionParser
import os.path
import re
import shutil
import subprocess
import sys
import string
import tarfile
import tempfile
import time
from zipfile import ZipFile

_log = logging.getLogger(__name__)
urgency = "low"
promote = "unstable"
standards_version = "3.8.2"
debhelper_compat = "5"  # below 5 is deprecated, latest is 7

debtransform = False
if os.path.isdir(".osc"):
    debtransform = True

# NOTE: the OBS will enable DEB_TRANSFORM only if there is any file named
#       debian.* in the sources area. Therefore the debian file must be
#       named "debian.diff.gz" for OBS and NOT "package.debian.diff.gz".
#       (check https://github.com/openSUSE/obs-build/blob/master/build
#       and look for "DEB_TRANSFORM=true" and its if-condition)
# NOTE: debtransform itself is also right there:
#       https://github.com/openSUSE/obs-build/blob/master/debtransform
#       https://github.com/openSUSE/obs-build/blob/master/debtransformbz2
# HINT: in order to debug debtransform problems, just download the scripts,
#       and run them in your sources directory (*tar, *.spec,*.dsc)
#       (rm -rf x; mkdir x; debtransform . *.dsc x/; cd x; dpkg-source -x *.dsc)

_nextfile = "--- "  # mark the start of the next file during diff generation
default_rpm_group = "System/Libraries"
# debian policy: Orphaned packages should have their Maintainer control field
# set to Debian QA Group <packages@qa.debian.org>
default_rpm_packager = "unknown <unknown@debian.org>"
default_rpm_license = "unknown"
default_package_architecture = "any"
package_importance = "optional"
_package_importances = ["required", "important",
                        "standard", "optional", "extra"]
check = True
strip = True

source_format = "1.0"  # "2.0" # "3.0 (quilt)" #
_source_formats = {
    "1": "1.0",
    "1.0": "1.0",
    "3": "3.0 (quilt)",
    "3.0": "3.0 (quilt)",
    "3.0 (quilt)": "3.0 (quilt)"
}

# %__mkdir_p              /bin/mkdir -p
# %__ln_s                 ln -s
usr_lib_rpm_macros = """# copy-n-paste from /usr/lib/rpm/macros
%_usr                   /usr
%_usrsrc                %{_usr}/src
%_var                   /var
%__cp                   /bin/cp
%__install              /usr/bin/install
%__mkdir                /bin/mkdir
%__mv                   /bin/mv
%__perl                 /usr/bin/perl
%__python               /usr/bin/python
%__rm                   /bin/rm
%__sed                  /usr/bin/sed
%__tar                  /bin/tar
%__unzip                /usr/bin/unzip

%_prefix                /usr
%_exec_prefix           %{_prefix}
%_bindir                %{_exec_prefix}/bin
%_sbindir               %{_exec_prefix}/sbin
%_libexecdir            %{_exec_prefix}/libexec
%_datadir               %{_prefix}/share
%_sysconfdir            /etc
%_sharedstatedir        %{_prefix}/com
%_localstatedir         %{_prefix}/var
%_lib                   lib
%_libdir                %{_exec_prefix}/%{_lib}
%_includedir            %{_prefix}/include
%_infodir               %{_datadir}/info
%_mandir                %{_datadir}/man
%_jvmdir                %{_libdir}/jvm
%_jvmjardir             %{_libdir}/jvm-exports
%_jvmprivdir            %{_libdir}/jvm-private

%_tmppath               %{_var}/tmp
%_docdir                %{_datadir}/doc
%make_install           make install DESTDIR=${RPM_BUILD_ROOT}
"""

debian_special_macros = """
%__make                 /usr/bin/make
%buildroot              ${CURDIR}/debian/tmp
%host                   $(DEB_HOST_GNU_TYPE)
%host_alias             $(DEB_HOST_GNU_TYPE)
%build                  $(DEB_BUILD_GNU_TYPE)
%build_alias            $(DEB_BUILD_GNU_TYPE)
"""

known_package_mapping = {
    "zlib-devel": "zlib1g-dev",
    "sdl-devel": "libsdl-dev",
    "sdl": "libsdl",
    "libopenssl-devel": "libssl-dev",
    "libidn-devel": "libidn11-dev",
    "glibc": "libc6",
    "curl-devel": "libcurl4-openssl-dev",
    "openssl-devel": "libssl-dev",
    "pcre-devel": "libpcre3-dev",
    "tightvnc": "tightvncserver",
    "boost-devel": "libboost-dev",
    "gflags-devel": "libgflags-dev",
    "glog-devel": "libgoogle-glog-dev",
    "protobuf-devel": "libprotobuf-dev",
    "gcc-c++": "g++"
}


class RpmSpecToDebianControl:
    on_comment = re.compile("^#.*")

    def __init__(self):
        self.debian_file = None
        self.source_orig_file = None
        self.packages = {}
        self.package = ""
        self.section = ""
        self.sectiontext = ""
        self.states = []
        self.var = {"autoreqprov": "yes"}
        self.typed = {"autoreqprov": "global"}
        self.rpm_macros = []
        self.urgency = urgency
        self.promote = promote
        self.package_importance = package_importance
        self.standards_version = standards_version
        self.debhelper_compat = debhelper_compat
        self.source_format = source_format
        self.debtransform = debtransform
        self.check = check
        self.strip = strip
        self.scan_macros(usr_lib_rpm_macros, "default")
        self.scan_macros(debian_special_macros, "debian")
        self.cache_packages2 = []
        self.cache_version = None
        self.cache_revision = None

    def has_names(self):
        return list(self.var.keys())

    def has_rpm_macros(self):
        return self.rpm_macros

    def is_default(self, name):
        if self.has(name) and self.typed[name] == 'default':
            return True
        return False

    def has(self, name):
        if name in self.var:
            return True
        return False

    def get(self, name, default=None):
        if self.package and self.package != "%{name}":
            values = self.packages[self.package].get(name, [])
            if len(values) > 0:
                return self.expand(values[0])

        if name in self.var:
            return self.var[name]
        return default

    def set(self, name, value, typed):
        if name in self.var:
            if self.typed[name] == "debian":
                _log.debug("ignore %s var '%s'", self.typed[name], name)
                return
            if self.var[name] != value:
                _log.info("override %s %s %s (was %s)",
                          typed, name, value, self.var[name])
        self.var[name] = value
        self.typed[name] = typed
        if typed == "default":
            self.rpm_macros.append(name)
        return self

    def scan_macros(self, text, typed):
        definition = re.compile("\s*[%](\w+)\s+(.*)")
        for line in text.split("\n"):
            found = definition.match(line)
            if found:
                name, value = found.groups()
                self.set(name, value.strip(), typed)
        return self
    # ========================================================= PARSER

    def state(self):
        if not self.states:
            return None
        return self.states[0]

    def set_source_format(self, value):
        if value in _source_formats:
            self.source_format = _source_formats[value]
            _log.info("using source format '%s'" % self.source_format)
        elif value:
            _log.fatal("unknown source format: '%s'" % value)

    def set_package_importance(self, value):
        if value in _package_importances:
            self.package_importance = value
            _log.info("using package importance '%s'" %
                      self.package_importance)
        elif value:
            _log.fatal("unknown package_importance: '%s'" % value)

    def new_state(self, state):
        if not self.states:
            self.states = [""]
        self.states[0] = state

    # %files -n explicit-package-name
    on_explicit_package = re.compile(r"-n\s+(\S+)")
    # %files -f list-of-files
    on_files_file = re.compile(r"-f\s+(\S+)")

    def new_package(self, package, options):
        package = package or ""
        options = options or ""
        found = self.on_explicit_package.search(options)
        if found:
            self.package = found.group(1)
        else:
            name = package.strip()
            if name:
                self.package = "%{name}-"+name
            else:
                self.package = "%{name}"
        self.packages.setdefault(self.package, {})

        # is there a -f flag with the list of files?
        found = self.on_files_file.search(options)
        if found:
            # a workaround. the file containing the list of files to install does not exist yet.
            # It will only exist after the %install section.
            # therefore after the install section; append that file to the list of files to install
            # NOTE: the install section exists only for the main package.
            self.packages["%{name}"].setdefault("%install", []).append("""
#spec2deb inserted:
set +x
while read line; do
    if [[ $line =~ %dir ]]; then
        line=${{line#*/}} # remove %dir prefix and leading /
        echo "$line" >> debian/{package_name}.dirs
    else
        line=${{line#/}} # remove leading /
        echo "$line" >> debian/{package_name}.install
    fi
done < {files}""".format(
                files=found.group(1), package_name=self.deb_package_name(self.expand(self.package))))

    on_requires = re.compile(
        r"([\w.+_-]+(\s+(=>|>=|>|<|=<|<=|=|==)\s+(\w+:)?[\w.~+_-]+)?)")

    def append_setting(self, name, value):
        package_sections = ["requires", "buildrequires", "prereq",
                            "provides", "conflicts", "suggests", "obsoletes"]
        value = self.expand(value.strip())
        if name in package_sections:
            requires = self.on_requires.findall(value)
            for require in requires:
                if name == "obsoletes":
                    self.packages[self.package].setdefault(
                        "conflicts", []).append(require[0])
                    self.packages[self.package].setdefault(
                        "replaces", []).append(require[0])
                else:
                    self.packages[self.package].setdefault(
                        name, []).append(require[0])
        else:
            self.packages[self.package].setdefault(name, []).append(value)
        # also provide the setting for macro expansion:
        if not self.package or self.package == "%{name}":
            if not name.startswith("%"):
                name1 = name.lower()
                if name1 in ["source", "patch"]:
                    name1 += "0"
                if name1 not in package_sections:
                    self.set(name1, value, "package")
            else:
                _log.debug("ignored to add a setting '%s'", name)
        else:
            if name not in package_sections:
                _log.debug(
                    "ignored to add a setting '%s' from package '%s'", name, self.package)

    def new_section(self, section, text=""):
        self.section = section.strip()
        self.sectiontext = text

    def append_section(self, text=None):
        self.sectiontext += text or ""

    on_variable = re.compile(r"\s*%(define|global)\s+(\S+)\s+(.*)")

    def save_variable(self, found_variable):
        typed, name, value = found_variable.groups()
        self.set(name.strip(), value.strip(), typed)

    on_architecture = re.compile(r"buildarch\s*:\s*(\S.*)", re.IGNORECASE)

    def save_architecture(self, found_architecture):
        value, = found_architecture.groups()
        if value == 'noarch':
            value = 'all'
        self.append_setting("architecture", value)

    on_setting = re.compile(r"\s*(\w+)\s*:\s*(\S.*)")

    def save_setting(self, found_setting):
        name, value = found_setting.groups()
        self.append_setting(name.lower(), value)

    on_new_if = re.compile(r"%if\b(.*)")
    on_else = re.compile(r"%else\b(.*)")
    on_end_if = re.compile(r"%endif\b(.*)")

    def new_if(self, found_new_if):
        condition, = found_new_if.groups()
        condition = self.expand(condition)
        condition_result = False
        try:
            condition_result = eval(condition)
        except SyntaxError:
            print('SyntaxError exception during evaluation of ' + condition)
        if condition_result:
            self.states.append("keep-if")
        else:
            self.states.append("skip-if")

    def new_else(self):
        if self.states[-1] == "skip-if":
            self.states[-1] = "keep-if"
        elif self.states[-1] == "keep-if":
            self.states[-1] = "skip-if"
        else:
            _log.error("unmatched %else with no preceding %if")

    def end_if(self):
        if self.states[-1] in ["skip-if", "keep-if"]:
            self.states = self.states[:-1]
        else:
            _log.error("unmatched %endif with no preceding %if")

    def skip_if(self):
        if "skip-if" in self.states:
            return True
        return False

    on_default_var1 = re.compile(
        r"\s*%\{!\?(\w+):\s+%(define|global)\s+\1\b(.*)\}")

    def default_var1(self, found_default_var):
        name, typed, value = found_default_var.groups()
        if not self.has(name):
            self.set(name.strip(), value.strip(), typed)
        else:
            _log.debug("override %%%s %s %s", typed, name, value)
        if typed != "global":
            _log.warning(
                "do not use %%define in default-variables, use %%global %s", name)

    on_default_var2 = re.compile(
        r"\s*[%][{][!][?](\w+)[:]\s*[%][{][?](\w+)[:]\s*[%](define|global)\s+\1\b(.*)[}][}]")

    def default_var2(self, found_default_var):
        name, name2, typed, value = found_default_var.groups()
        if not self.has(name2):
            return
        if not self.has(name):
            self.set(name, value.strip(), typed)
        else:
            _log.debug("override %%%s %s %s", typed, name, value)
        if typed != "global":
            _log.warning(
                "do not use %%define in default-variables, use %%global %s", name)

    on_default_var3 = re.compile(
        r"\s*[%][{][!][?](\w+)[:]\s*[%][{][?](\w+)[:]\s*[%](define|global)\s+\1\b(.*)[}][}]")

    def default_var3(self, found_default_var):
        name, name3, typed, value = found_default_var.groups()
        if self.has(name3):
            return
        if not self.has(name):
            self.set(name, value.strip(), typed)
        else:
            _log.debug("override %%%s %s %s", typed, name, value)
        if typed != "global":
            _log.warning(
                "do not use %%define in default-variables, use %%global %s", name)

    # %package [ -n package-name ] [ subpackage ]
    on_package = re.compile(r"%(package)\b(?:\s+([^-]\S*))?(?:\s+(-.*))?")

    def start_package(self, found_package):
        _, package, options = found_package.groups()
        self.new_package(package, options)
        self.new_state("package")

    # %description [ -n package-name ] [ subpackage ]
    on_description = re.compile(
        r"%(description)\b(?:\s+([^-]\S*))?(?:\s+(-.*))?")

    def start_description(self, found_description):
        rule, package, options = found_description.groups()
        self.new_package(package, options)
        self.new_section("%"+rule.strip())
        self.new_state("description")

    def endof_description(self):
        self.append_setting(self.section, self.sectiontext)

    on_changelog = re.compile(r"%(changelog)(\s*)")

    def start_changelog(self, found_changelog):
        rule, options = found_changelog.groups()
        self.new_package("", options)
        self.new_section("%"+rule.strip())
        self.new_state("changelog")

    def endof_changelog(self):
        self.append_setting(self.section, self.sectiontext)

    on_rules = re.compile(r"%(prep|build|install|check|clean)\b(?:\s+(-.*))?")

    def start_rules(self, found_rules):
        rule, options = found_rules.groups()
        self.new_package("", options)
        self.section = rule.strip()
        self.new_section("%"+rule.strip())
        self.new_state("rules")

    def endof_rules(self):
        self.append_setting(self.section, self.sectiontext)

    on_scripts = re.compile(
        r"%(post|postun|pre|preun)\b(?:\s+([^-]\S*))?(?:\s+(-.*))?")

    def start_scripts(self, found_scripts):
        rule, package, options = found_scripts.groups()
        self.new_package(package, options)
        self.new_section("%"+rule.strip())
        self.new_state("scripts")

    def endof_scripts(self):
        self.append_setting(self.section, self.sectiontext)

    # %files [ -f /path/to/filename ] [ subpackage ]
    on_files = re.compile(r"%(files)\b(?:\s+([^-]\S*))?(?:\s+(-.*))?")

    def start_files(self, found_files):
        rule, package, options = found_files.groups()
        self.new_package(package, options)
        self.new_section("%"+rule)
        self.new_state("files")

    def endof_files(self):
        self.append_setting(self.section, self.sectiontext)

    on_debug_package = re.compile(r"%(debug_package)(\s*)")

    def set_debug_package(self):
        _log.warning(
            "Debug package detected but still not handled.")

    on_ghost = re.compile(r"%(ghost)\s.*")

    def parse(self, rpmspec):
        default = "%package "
        found_package = self.on_package.match(default)
        assert found_package
        self.start_package(found_package)
        for line in io.open(rpmspec, 'r', encoding='utf8'):
            if self.state() in ["package"]:
                found_default_var1 = self.on_default_var1.match(line)
                found_default_var2 = self.on_default_var2.match(line)
                found_new_if = self.on_new_if.match(line)
                found_else = self.on_else.match(line)
                found_end_if = self.on_end_if.match(line)
                found_comment = self.on_comment.match(line)
                found_variable = self.on_variable.match(line)
                found_architecture = self.on_architecture.match(line)
                found_setting = self.on_setting.match(line)
                found_package = self.on_package.match(line)
                found_description = self.on_description.match(line)
                found_rules = self.on_rules.match(line)
                found_scripts = self.on_scripts.match(line)
                found_files = self.on_files.match(line)
                found_changelog = self.on_changelog.match(line)
                found_debug_package = self.on_debug_package.match(line)
                if found_comment:
                    # ignore comments
                    pass
                elif found_default_var1:
                    self.default_var1(found_default_var1)
                elif found_default_var2:
                    self.default_var2(found_default_var2)
                elif found_new_if:
                    self.new_if(found_new_if)
                elif found_else:
                    self.new_else()
                elif found_end_if:
                    self.end_if()
                elif self.skip_if():
                    continue
                elif found_variable:
                    self.save_variable(found_variable)
                elif found_architecture:
                    self.save_architecture(found_architecture)
                elif found_setting:
                    self.save_setting(found_setting)
                elif found_package:
                    self.start_package(found_package)
                elif found_description:
                    self.start_description(found_description)
                elif found_rules:
                    self.start_rules(found_rules)
                elif found_scripts:
                    self.start_scripts(found_scripts)
                elif found_files:
                    self.start_files(found_files)
                elif found_changelog:
                    self.start_changelog(found_changelog)
                elif found_debug_package:
                    self.set_debug_package()
                elif line.strip():
                    # line is not empty...
                    _log.error("%s unmatched line:\n %s", self.state(), line)
            elif self.state() in ["description"]:
                found_new_if = self.on_new_if.match(line)
                found_else = self.on_else.match(line)
                found_end_if = self.on_end_if.match(line)
                found_package = self.on_package.match(line)
                found_description = self.on_description.match(line)
                found_rules = self.on_rules.match(line)
                found_scripts = self.on_scripts.match(line)
                found_files = self.on_files.match(line)
                found_changelog = self.on_changelog.match(line)
                found_debug_package = self.on_debug_package.match(line)
                if (found_package or found_description or found_rules or found_scripts
                        or found_files or found_changelog or found_debug_package):
                    self.endof_description()
                if found_new_if:
                    self.new_if(found_new_if)
                elif found_else:
                    self.new_else()
                elif found_end_if:
                    self.end_if()
                elif self.skip_if():
                    continue
                elif found_package:
                    self.start_package(found_package)
                elif found_description:
                    self.start_description(found_description)
                elif found_rules:
                    self.start_rules(found_rules)
                elif found_scripts:
                    self.start_scripts(found_scripts)
                elif found_files:
                    self.start_files(found_files)
                elif found_changelog:
                    self.start_changelog(found_changelog)
                elif found_debug_package:
                    self.set_debug_package()
                else:
                    self.append_section(line)
            elif self.state() in ["rules"]:
                found_new_if = self.on_new_if.match(line)
                found_else = self.on_else.match(line)
                found_end_if = self.on_end_if.match(line)
                found_variable = self.on_variable.match(line)
                found_package = self.on_package.match(line)
                found_description = self.on_description.match(line)
                found_rules = self.on_rules.match(line)
                found_scripts = self.on_scripts.match(line)
                found_files = self.on_files.match(line)
                found_changelog = self.on_changelog.match(line)
                found_debug_package = self.on_debug_package.match(line)
                if (found_package or found_description or found_rules or found_scripts
                        or found_files or found_changelog or found_debug_package):
                    self.endof_files()
                if found_new_if:
                    self.new_if(found_new_if)
                elif found_else:
                    self.new_else()
                elif found_end_if:
                    self.end_if()
                elif self.skip_if():
                    continue
                elif found_variable:
                    self.save_variable(found_variable)
                elif found_package:
                    self.start_package(found_package)
                elif found_description:
                    self.start_description(found_description)
                elif found_rules:
                    self.start_rules(found_rules)
                elif found_scripts:
                    self.start_scripts(found_scripts)
                elif found_files:
                    self.start_files(found_files)
                elif found_changelog:
                    self.start_changelog(found_changelog)
                elif found_debug_package:
                    self.set_debug_package()
                else:
                    self.append_section(line)
            elif self.state() in ["scripts"]:
                found_new_if = self.on_new_if.match(line)
                found_else = self.on_else.match(line)
                found_end_if = self.on_end_if.match(line)
                found_package = self.on_package.match(line)
                found_description = self.on_description.match(line)
                found_rules = self.on_rules.match(line)
                found_scripts = self.on_scripts.match(line)
                found_files = self.on_files.match(line)
                found_changelog = self.on_changelog.match(line)
                found_debug_package = self.on_debug_package.match(line)
                if (found_package or found_description or found_rules or found_scripts
                        or found_files or found_changelog or found_debug_package):
                    self.endof_scripts()
                if found_new_if:
                    self.new_if(found_new_if)
                elif found_else:
                    self.new_else()
                elif found_end_if:
                    self.end_if()
                elif self.skip_if():
                    continue
                elif found_package:
                    self.start_package(found_package)
                elif found_description:
                    self.start_description(found_description)
                elif found_rules:
                    self.start_rules(found_rules)
                elif found_scripts:
                    self.start_scripts(found_scripts)
                elif found_files:
                    self.start_files(found_files)
                elif found_changelog:
                    self.start_changelog(found_changelog)
                elif found_debug_package:
                    self.set_debug_package()
                else:
                    self.append_section(line)
            elif self.state() in ["files"]:
                found_new_if = self.on_new_if.match(line)
                found_else = self.on_else.match(line)
                found_end_if = self.on_end_if.match(line)
                found_package = self.on_package.match(line)
                found_description = self.on_description.match(line)
                found_rules = self.on_rules.match(line)
                found_scripts = self.on_scripts.match(line)
                found_files = self.on_files.match(line)
                found_ghost = self.on_ghost.match(line)
                found_changelog = self.on_changelog.match(line)
                found_debug_package = self.on_debug_package.match(line)
                if (found_package or found_description or found_rules or found_scripts
                        or found_files or found_changelog or found_debug_package):
                    self.endof_files()
                if found_new_if:
                    self.new_if(found_new_if)
                elif found_else:
                    self.new_else()
                elif found_end_if:
                    self.end_if()
                elif self.skip_if():
                    continue
                elif found_package:
                    self.start_package(found_package)
                elif found_description:
                    self.start_description(found_description)
                elif found_rules:
                    self.start_rules(found_rules)
                elif found_scripts:
                    self.start_scripts(found_scripts)
                elif found_ghost:
                    print("skipping ghost line in files section")
                    continue
                elif found_files:
                    self.start_files(found_files)
                elif found_changelog:
                    self.start_changelog(found_changelog)
                elif found_debug_package:
                    self.set_debug_package()
                else:
                    line = line.replace("(noreplace)", "")
                    self.append_section(line)
            elif self.state() in ["changelog"]:
                found_package = self.on_package.match(line)
                found_description = self.on_description.match(line)
                found_rules = self.on_rules.match(line)
                found_scripts = self.on_scripts.match(line)
                found_files = self.on_files.match(line)
                found_changelog = self.on_changelog.match(line)
                found_debug_package = self.on_debug_package.match(line)
                if (found_package or found_description or found_rules or found_scripts
                        or found_files or found_changelog or found_debug_package):
                    self.endof_description()
                if found_package:
                    self.start_package(found_package)
                elif found_description:
                    self.start_description(found_description)
                elif found_rules:
                    self.start_rules(found_rules)
                elif found_scripts:
                    self.start_scripts(found_scripts)
                elif found_files:
                    self.start_files(found_files)
                elif found_changelog:
                    self.start_changelog(found_changelog)
                elif found_debug_package:
                    self.set_debug_package()
                else:
                    self.append_section(line)
            else:
                _log.fatal("UNKNOWN state %s", self.states)
        # for line
        if self.skip_if():
            self.error("end of while in skip-if section")
        if self.state() in ["package"]:
            # nothing to do...
            pass
        elif self.state() in ["description"]:
            self.endof_description()
        elif self.state() in ["rules"]:
            self.endof_rules()
        elif self.state() in ["scripts"]:
            self.endof_scripts()
        elif self.state() in ["files"]:
            self.endof_files()
        elif self.state() in ["changelog"]:
            self.endof_changelog()
        else:
            _log.fatal("UNKNOWN state %s (at end of file)", self.states)

    on_embedded_name = re.compile(r"[%](\w+)\b")
    on_required_name = re.compile(r"[%][{](\w+)[}]")
    on_optional_name = re.compile(r"[%][{]([!]?[?])(\w+):?(\w+)?[}]")

    def expand(self, text):
        orig = text
        lines = text.split('\n')
        for line_index in range(len(lines)):
            line = lines[line_index]
            for _ in range(100):
                oldline = line
                line = line.replace("%%", "\1")
                for found in self.on_embedded_name.finditer(line):
                    name, = found.groups()
                    if name == 'setup' or name == 'defattr' or name == 'dir' or name == 'attr' or name == 'config':
                        continue
                    elif self.has(name):
                        value = self.get(name)
                        line = re.sub("%"+name+"\\b", value, line)
                    else:
                        _log.error("unable to expand %%%s in: %s", name, line)
                line = line.replace("%%", "\1")
                for found in self.on_required_name.finditer(line):
                    name, = found.groups()
                    if self.has(name):
                        value = self.get(name)
                        line = re.sub("%{"+name+"}", value, line)
                    else:
                        _log.error(
                            "unable to expand %%{%s} in : %s", name, line)
                line = line.replace("%%", "\1")
                for found in self.on_optional_name.finditer(line):
                    mark, name, replacement = found.groups()

                    to_replace = "%{"
                    to_replace += "\?" if (mark == '?') else "!\?"
                    to_replace += name
                    if (replacement):
                        to_replace += ":" + replacement
                    to_replace += "}"
                    value = ''

                    if (self.has(name) and mark == '?') or (not self.has(name) and mark == '!?'):
                        value = replacement if replacement else self.get(name)
                        line = re.sub(to_replace, value, line)

                    line = re.sub(to_replace, value, line)
                line = line.replace("\1", "%%")
                if line == oldline:
                    break
            lines[line_index] = line
        text = "\n".join(lines)
        if "$(" in text and orig not in ["%buildroot", "%__make"]:
            _log.warning(
                "expand of '%s' left a make variable:\n %s", orig, text)
        return text

    def deb_packages(self):
        for deb, _ in self.deb_packages2():
            yield deb

    def deb_packages2(self):
        if self.cache_packages2:
            for item in self.cache_packages2:
                yield item
        else:
            for item in self._deb_packages2():
                self.cache_packages2.append(item)
                yield item

    def _deb_packages2(self):
        for package in sorted(self.packages):
            deb_package = package
            # if deb_package == "%{name}" and len(self.packages) > 1:
            # deb_package = "%{name}-bin"
            deb_package = self.deb_package_name(self.expand(deb_package))
            yield deb_package, package

    def deb_package_name(self, package):
        """ debian.org/doc/debian-policy/ch-controlfields.html##s-f-Source
                ... must consist only of lower case letters (a-z), digits (0-9),
                plus (+) and minus (-) signs, and periods (.), must be at least
                two characters long and must start with an alphanumeric. """
        if not package.startswith("${"):
            package = package.lower().replace("_", "")
        if package in known_package_mapping:
            package = known_package_mapping[package]
        elif package.endswith("-devel"):
            package = package[:-2]
        return package

    def deb_build_depends(self):
        depends = ["debhelper (>= %s)" % self.debhelper_compat]
        for package in self.packages:
            for buildrequires in self.packages[package].get("buildrequires", []):
                depend = self.deb_requires(buildrequires)
                if depend not in depends:
                    depends.append(depend)
        return depends

    def deb_requires(self, requires):
        requires = self.expand(requires)
        withversion = re.match(
            "(\S+)\s+(=>|>=|>|<|=<|<=|=|==)\s+(\S+)", requires)
        if withversion:
            package, relation, version = withversion.groups()
            if relation == "<":
                relation = "<<"
            elif relation == ">":
                relation = ">>"
            elif relation == "=>":
                relation = ">="
            elif relation == "=<":
                relation = "<="
            elif relation == "==":
                relation = "="
            deb_package = self.deb_package_name(package)
            return "%s (%s %s)" % (deb_package, relation, version)
        else:
            deb_package = self.deb_package_name(requires.strip())
            return deb_package

    def deb_provides(self, provides):
        provides = self.expand(provides)
        withversion = re.match(
            "(\S+)\s+(=>|>=|>|<|=<|<=|=|==)\s+(\S+)", provides)
        if withversion:
            package, relation, version = withversion.groups()
            if relation == "<":
                relation = "<<"
            elif relation == ">":
                relation = ">>"
            elif relation == "=>":
                relation = ">="
            elif relation == "=<":
                relation = "<="
            elif relation == "==":
                relation = "="
            deb_package = self.deb_package_name(package)
            return "%s (%s %s)" % (deb_package, relation, version)
        else:
            deb_package = self.deb_package_name(provides.strip())
            return "%s (= %s)" % (deb_package, self.deb_version())

    def deb_sourcefile(self):
        sourcefile = self.get("source", self.get("source0"))
        x = sourcefile.rfind("/")
        if x:
            sourcefile = sourcefile[x+1:]
        return sourcefile

    def deb_source(self, sourcefile=None):
        return self.get("name")

    def deb_src(self):
        return self.deb_source()+"-"+self.deb_version()

    def deb_version(self):
        if self.cache_version is None:
            value = self.get("version", "0")
            self.cache_version = self.expand(value)
        return self.cache_version

    def deb_revision_with_epoch(self):
        epoch = self.get("epoch", None)
        return self.expand(epoch) + ":" + self.deb_revision() if epoch else self.deb_revision()

    def deb_revision(self):
        if self.cache_revision is None:
            release = self.get("release", "0")
            value = self.deb_version()+"-"+release
            self.cache_revision = self.expand(value)
        return self.cache_revision

    def debian_dsc(self, nextfile=_nextfile, into=None):
        yield nextfile+"debian/dsc"
        yield "+Format: %s" % self.source_format
        sourcefile = self.deb_sourcefile()
        source = self.deb_source(sourcefile)
        yield "+Source: %s" % self.expand(source)
        binaries = list(self.deb_packages())
        yield "+Binary: %s" % ", ".join(binaries)
        yield "+Architecture: %s" % self.get("architecture", [default_package_architecture])[0]
        yield "+Version: %s" % self.deb_revision_with_epoch()
        yield "+Maintainer: %s" % self.get("packager", default_rpm_packager)
        yield "+Standards-Version: %s" % self.standards_version
        yield "+Homepage: %s" % self.get("url", "")
        depends = list(self.deb_build_depends())
        yield "+Build-Depends: %s" % ", ".join(depends)
        source_file = self.expand(sourcefile)
        debian_file = self.debian_file
        if not debian_file:
            debian_file = "%s.debian.tar.gz" % (self.expand(source))
        if self.debtransform:
            yield "+Debtransform-Tar: %s" % source_file
            if ".tar." in debian_file:
                yield "+Debtransform-Files-Tar: %s" % debian_file
        else:
            source_orig = self.source_orig_file or source_file
            source_orig_path = os.path.join(into or "", source_orig)
            debian_file_path = os.path.join(into or "", debian_file)
            source_orig_md5sum = self.md5sum(source_orig_path)
            debian_file_md5sum = self.md5sum(debian_file_path)
            if os.path.exists(source_orig_path):
                source_orig_size = os.path.getsize(source_orig_path)
                _log.debug("source_orig '%s' size %s",
                           source_orig_path, source_orig_size)
            else:
                source_orig_size = 0
                _log.info("source_orig '%s' not found", source_orig_path)
            if os.path.exists(debian_file_path):
                debian_file_size = os.path.getsize(debian_file_path)
                _log.debug("debian_file '%s' size %s",
                           debian_file_path, debian_file_size)
            else:
                debian_file_size = 0
                _log.info("debian_file '%s' not found", debian_file_path)
            yield "+Files: %s" % ""
            yield "+ %s %i %s" % (source_orig_md5sum, source_orig_size, source_orig)
            yield "+ %s %s %s" % (debian_file_md5sum, debian_file_size, debian_file)

    def md5sum(self, filename):
        if not os.path.exists(filename):
            return "0" * 32
        with open(filename, mode='rb') as f:
            d = hashlib.md5()
            for buf in iter(partial(f.read, 128), b''):
                d.update(buf)
        return d.hexdigest()

    def group2section(self, group):
        # there are 3 areas ("main", "contrib", "non-free") with multiple
        # sections. http://packages.debian.org/unstable/ has a list of all
        # sections that are currently used. - For Opensuse the current group
        # list is at http://en.opensuse.org/openSUSE:Package_group_guidelines
        debian = {
            "admin": [],
            "cli-mono": [],
            "comm": [],
            "database": ["Productivity/Database"],
            "debian-installer": [],
            "debug": ["Development/Tools/Debuggers"],
            "devel": ["Development/Languages/C and C++"],
            "doc": ["Documentation"],
            "editors": [],
            "electronics": [],
            "embedded": [],
            "fonts": [],
            "games": ["Amusements/Game"],
            "gnome": ["System/GUI/GNOME"],
            "gnu-r": [],
            "gnustep": ["System/GUI/Other"],
            "graphics": ["Productivity/Graphics"],
            "hamradio": ["Productivity/Hamradio"],
            "haskell": [],
            "httpd": ["Productivity/Networking/Web"],
            "interpreters": ["Development/Languages/Other"],
            "java": ["Development/Languages/Java"],
            "kde": ["System/GUI/KDE"],
            "kernel": ["System/Kernel"],
            "libdevel": ["Development/Tool"],
            "libs": ["Development/Lib", "System/Lib"],
            "lisp": [],
            "localization": ["System/Localization", "System/i18n"],
            "mail": ["Productivity/Networking/Email"],
            "math": ["Amusements/Teaching/Math", "Productivity/Scientific/Math"],
            "misc": [],
            "net": ["Productivity/Networking/System"],
            "news": ["Productivity/Networking/News"],
            "ocaml": [],
            "oldlibs": [],
            "othersofs": [],
            "perl": ["Development/Languages/Perl"],
            "php": [],
            "python": ["Development/Languages/Python"],
            "ruby": ["Development/Languages/Ruby"],
            "science": ["Productivity/Scientific"],
            "shells": ["System/Shell"],
            "sound": ["System/Sound"],
            "tex": ["Productivity/Publishing/TeX"],
            "text": ["Productivity/Publishing"],
            "utils": [],
            "vcs": ["Development/Tools/Version Control"],
            "video": [],
            "virtual": [],
            "web": [],
            "x11": ["System/X11"],
            "xfce": ["System/GUI/XFCE"],
            "zope": [],
        }
        if isinstance(group, list) and len(group) >= 1:
            group = group[0]
        for section, group_prefixes in list(debian.items()):
            for group_prefix in group_prefixes:
                if group.startswith(group_prefix):
                    return section
        # make a guess:
        if "Lib" in group:
            return "libs"
        elif "Network" in group:
            return "net"
        else:
            return "utils"

    def deb_description_lines(self, text, prefix="Description:"):
        if isinstance(text, list):
            text = "\n".join(text)
        for line in text.split("\n"):
            if not line.strip():
                yield prefix+" ."
            else:
                yield prefix+" "+line
            prefix = ""

    def debian_control(self, nextfile=_nextfile):
        yield nextfile+"debian/control"
        group = self.get("group", default_rpm_group)
        section = self.group2section(group)
        yield "+Priority: %s" % self.package_importance
        yield "+Maintainer: %s" % self.get("packager", default_rpm_packager)
        source = self.deb_source()
        yield "+Source: %s" % self.expand(source)
        depends = list(self.deb_build_depends())
        yield "+Build-Depends: %s" % ", ".join(depends)
        yield "+Standards-Version: %s" % self.standards_version
        yield "+Homepage: %s" % self.get("url", "")
        yield "+"
        for deb_package, package in sorted(self.deb_packages2()):
            if package == "%{name}-debuginfo":
                _log.info(
                    "NOT building debuginfo package on deb: dbgsym packages should be created automatically")
                continue
            if not self.packages[package].get("%files"):
                _log.warning(
                    "Package %s doesn't have a %%files section, won't build", deb_package)
                continue
            yield "+Package: %s" % deb_package
            group = self.packages[package].get("group", default_rpm_group)
            section = self.group2section(group)
            yield "+Section: %s" % section
            yield "+Architecture: %s" % self.packages[package].get("architecture", [default_package_architecture])[0]
            depends = self.packages[package].get("requires", [])
            if self.get("autoreqprov") == "yes":
                depends.append("${shlibs:Depends}")
            depends.append("${misc:Depends}")
            provides = self.packages[package].get("provides", "")
            replaces = self.packages[package].get("replaces", "")
            conflicts = self.packages[package].get("conflicts", "")
            pre_depends = self.packages[package].get("prereq", "")
            if depends:
                deb_depends = [self.deb_requires(req) for req in depends]
                yield "+Depends: %s" % ", ".join(deb_depends)
            if provides:
                deb_provides = [self.deb_provides(req) for req in provides]
                yield "+Provides: %s" % ", ".join(deb_provides)
            if replaces:
                deb_replaces = [self.deb_requires(req) for req in replaces]
                yield "+Replaces: %s" % ", ".join(deb_replaces)
            if conflicts:
                deb_conflicts = [self.deb_requires(req) for req in conflicts]
                yield "+Conflicts: %s" % ", ".join(deb_conflicts)
            if pre_depends:
                deb_pre_depends = [self.deb_requires(
                    req) for req in pre_depends]
                yield "+Pre-Depends: %s" % ", ".join(deb_pre_depends)
            text = self.packages[package].get("%description", "")
            for line in self.deb_description_lines(text):
                yield "+"+self.expand(line)
            yield "+"

    def debian_copyright(self, nextfile=_nextfile):
        yield nextfile+"debian/copyright"
        yield "+License: %s" % self.get("license", default_rpm_license)

    def debian_install(self, nextfile=_nextfile):
        docs = []
        for deb_package, package in sorted(self.deb_packages2()):
            files_name = "debian/%s.install" % deb_package
            dirs_name = "debian/%s.dirs" % deb_package
            files_list = []
            dirs_list = []
            filesection = self.packages[package].get("%files", [""])
            if not isinstance(filesection, list):
                filesection = [filesection]
            # hack: we put commands with file permission modifications in the post section...
            postsection = self.packages[package].get("%post")
            if not postsection:
                self.packages[package]["%post"] = []
                postsection = self.packages[package].get("%post")
            # for each package we start again with the default file permissions
            package_file_permissions = '-'
            package_file_user = 'root'
            package_file_group = 'root'
            for files in filesection:
                for path in files.split("\n"):
                    # clean up path. dpkg -L will give clean paths, so this has to match exactly
                    path = re.sub('"', '', path.strip())
                    path = re.sub('/+', '/', path)
                    file_with_attr = {
                        "permissions": package_file_permissions,
                        "user": package_file_user,
                        "group": package_file_group,
                        "path": path,
                        "dir": False,
                        "doc": False}
                    while file_with_attr["path"].startswith("%"):
                        if file_with_attr["path"].startswith("%config"):
                            file_with_attr["path"] = file_with_attr["path"][len(
                                "%config"):].strip()
                            if not file_with_attr["path"].startswith("/etc/"):
                                _log.warning(
                                    "debhelpers will treat files in /etc/ as configs but not your '%s'", file_with_attr["path"])
                        elif file_with_attr["path"].startswith("%doc"):
                            file_with_attr["doc"] = True
                            file_with_attr["path"] = file_with_attr["path"][len(
                                "%doc"):].strip()
                        elif file_with_attr["path"].startswith("%dir"):
                            file_with_attr["dir"] = True
                            file_with_attr["path"] = file_with_attr["path"][len(
                                "%dir"):].strip()
                        elif file_with_attr["path"].startswith("%attr"):
                            parts = re.split('\(|,|\)', file_with_attr["path"])
                            file_with_attr["permissions"] = parts[1].strip()
                            file_with_attr["user"] = parts[2].strip()
                            file_with_attr["group"] = parts[3].strip()
                            file_with_attr["path"] = parts[4].strip()
                        elif file_with_attr["path"].startswith("%defattr"):
                            parts = re.split('\(|,|\)', file_with_attr["path"])
                            package_file_permissions = parts[1].strip()
                            package_file_user = parts[2].strip()
                            package_file_group = parts[3].strip()
                            file_with_attr["path"] = parts[4].strip()
                        else:
                            parts = file_with_attr["path"].split()
                            _log.warning(
                                "Warning: ingoring file prefix: " + parts[0])
                            file_with_attr["path"] = file_with_attr["path"][len(
                                parts[0]):].strip()
                    if file_with_attr["doc"]:
                        docs += [file_with_attr["path"]]
                    # The next lines might seem contradictory. Some word of explanation:
                    # - The %dir directive in a spec file = package the directory and NOT the files below
                    # Hence:
                    # - dir: NON-recursive changing of permissions and ownership
                    # - !dir: (which might be a file or directory; it just has not been marked with %dir in spec file): recursive!
                    elif file_with_attr["dir"]:
                        dirs_list.append(file_with_attr["path"])
                        if file_with_attr["permissions"] != '-':
                            postsection.append(
                                "chmod " + file_with_attr["permissions"] + " " + file_with_attr["path"] + " || true")
                        postsection.append(
                            "chown " + file_with_attr["user"] + ":" + file_with_attr["group"] + " " + file_with_attr["path"] + " || true")
                    elif len(file_with_attr["path"]):
                        files_list.append(file_with_attr["path"])
                        final_package_name = self.deb_package_name(
                            self.expand(package))
                        path_pattern = re.sub(
                            '\*', '.*', file_with_attr["path"])  # replace * with .*
                        # remove final /; otherwise directory permissions would not be changed
                        path_pattern = path_pattern.rstrip("/")
                        if file_with_attr["permissions"] != '-':
                            postsection.append("dpkg -L " + final_package_name + " | grep '^" +
                                               path_pattern + r"' | xargs -d '\n' -i chmod " + file_with_attr["permissions"] + " {} || true")
                        postsection.append("dpkg -L " + final_package_name + " | grep '^" + path_pattern +
                                           r"' | xargs -d '\n' -i chown " + file_with_attr["user"] + ":" + file_with_attr["group"] + " {} || true")
            if dirs_list:
                yield nextfile+dirs_name
                for path in dirs_list:
                    path = self.expand(path)
                    if path.startswith("/"):
                        path = path[1:]
                    yield "+"+path
            if files_list:
                yield nextfile+files_name
                for path in files_list:
                    path = self.expand(path)
                    if path.startswith("/"):
                        path = path[1:]
                    yield "+"+path
        if docs:
            yield nextfile+"docs"
            for doc in docs:
                for path in doc.split(" "):
                    path = self.expand(path.strip())
                    if path.startswith("/"):
                        path = path[1:]
                    if path:
                        yield "+"+path

    def debian_changelog(self, nextfile=_nextfile):
        name = self.expand(self.get("name"))
        version = self.expand(self.deb_revision_with_epoch())
        packager = self.expand(self.get("packager", default_rpm_packager))
        promote = self.promote
        urgency = self.urgency
        yield nextfile+"debian/changelog"
        yield "+%s (%s) %s; urgency=%s" % (name, version, promote, urgency)
        yield "+"
        yield "+  * generated OBS deb build"
        yield "+"
        yield "+ -- %s  Mon, 25 Dec 2007 10:50:38 +0100" % (packager)

    def debian_rules(self, nextfile=_nextfile):
        yield nextfile + "debian/compat"
        yield "+%s" % self.debhelper_compat

        yield nextfile+"debian/vars"
        yield "+RPM_BUILD_ROOT=$(pwd)/debian/tmp"
        yield "+CURDIR=$(pwd)"
        for name in self.has_rpm_macros():
            if name.startswith("_"):
                value = self.get(name)
                value2 = re.sub(r"[%][{](\w+)[}]", r"${\1}", value)
                yield "+%s=%s" % (name, value2)
        for name in self.has_names():
            if name.startswith("_") and not self.is_default(name):
                value = self.get(name)
                value2 = re.sub(r"[%][{](\w+)[}]", r"${\1}", value)
                yield "+%s=%s" % (name, value2)

        yield nextfile+"debian/prep.sh"
        yield "+#!/bin/bash"
        yield "+. debian/vars"
        yield "+set -e"
        yield "+set -x"
        for line in self.deb_script("%prep"):
            yield "+\t"+line

        yield nextfile+"debian/build.sh"
        yield "+#!/bin/bash"
        yield "+. debian/vars"
        yield "+set -e"
        yield "+set -x"
        for line in self.deb_script("%build"):
            yield "+\t"+line
        if self.check:
            yield "+echo spec2deb inserted check section after build."
            for line in self.deb_script("%check"):
                yield "+"+line

        yield nextfile+"debian/install.sh"
        yield "+#!/bin/bash"
        yield "+. debian/vars"
        yield "+set -e"
        yield "+set -x"
        for line in self.deb_script("%install"):
            yield "+"+line

        yield nextfile+"debian/rules"
        yield "+#!/usr/bin/make -f"
        yield "+# -*- makefile -*-"
        yield "+# Uncomment this to turn on verbose mode."
        yield "+#export DH_VERBOSE=1"
        yield "+"
        yield "+# These are used for cross-compiling and for saving the configure script"
        yield "+# from having to guess our platform (since we know it already)"
        yield "+DEB_HOST_GNU_TYPE  ?= $(shell dpkg-architecture -qDEB_HOST_GNU_TYPE)"
        yield "+DEB_BUILD_GNU_TYPE ?= $(shell dpkg-architecture -qDEB_BUILD_GNU_TYPE)"
        yield "+"
        yield "+"
        yield "+CFLAGS = -Wall -g"
        yield "+"
        yield "+ifneq (,$(findstring noopt,$(DEB_BUILD_OPTIONS)))"
        yield "+           CFLAGS += -O0"
        yield "+else"
        yield "+           CFLAGS += -O2"
        yield "+endif"
        yield "+ifeq (,$(findstring nostrip,$(DEB_BUILD_OPTIONS)))"
        yield "+           INSTALL_PROGRAM += -s"
        yield "+endif"
#                yield "+"
#                for name in self.has_names():
#                        if name.startswith("_"):
#                                value = self.get(name)
#                                value2 = re.sub(r"[%][{](\w+)[}]", r"$(\1)", value)
#                                yield "+%s=%s" % (name, value2)
        yield "+"
        yield "+configure: configure-stamp"
        yield "+configure-stamp:"
        yield "+\tdh_testdir"
        yield "+\tbash debian/prep.sh"
#                for line in self.deb_script("%prep"):
#                        yield "+\t"+line
        yield "+\t#"
        yield "+\ttouch configure-stamp"
        yield "+"
        yield "+build: build-stamp"
        yield "+build-stamp: configure-stamp"
        yield "+\tdh_testdir"
        yield "+\tbash debian/build.sh"
#                for line in self.deb_script("%build"):
#                        yield "+\t"+line
        yield "+\t#"
        yield "+\ttouch build-stamp"
        yield "+"
        yield "+clean:"
        yield "+\tdh_testdir"
        yield "+\tdh_testroot"
        yield "+\trm -f configure-stamp build-stamp"
        yield "+\tdh_auto_clean"
        yield "+\tdh_clean"
        yield "+"
        yield "+install: build"
        yield "+\tdh_testdir"
        yield "+\tdh_testroot"
        yield "+\tdh_prep"
        yield "+\tdh_installdirs"
        yield "+\t# Add here commands to install the package into debian/tmp"
        yield "+\tmkdir -p debian/tmp"
        # +                  $(MAKE) install DESTDIR=$(CURDIR)/debian/tmp
        yield "+\tbash debian/install.sh"
#                for line in self.deb_script("%install"):
#                        yield "+\t"+line
        yield "+\t# Move all files in their corresponding package"
        yield "+\tdh_install --list-missing --fail-missing --sourcedir=debian/tmp"
        yield "+\t# empty dependency_libs in .la files"
        yield "+\tfind debian/ -name '*.la' -exec sed -i \"/dependency_libs/ s/'.*'/''/\" {} \;"
        yield "+"
        yield "+# Build architecture-independent files here."
        yield "+binary-indep: build install"
        yield "+# We have nothing to do by default."
        yield "+"
        yield "+# Build architecture-dependent files here."
        yield "+binary-arch: build install"
        yield "+\tdh_testdir"
        yield "+\tdh_testroot"
        # "+\tdh_installchangelogs ChangeLog"
        yield "+\tdh_installdocs"
        yield "+\tdh_installexamples"
        yield "+\tdh_installman"
        yield "+\tdh_link"
        if self.strip:
            yield "+\tdh_strip"
        yield "+\tdh_compress"
        # "+\tdh_fixperms"
        if self.get("autoreqprov") == "yes":
            yield "+\tdh_makeshlibs -V"
        yield "+\tdh_installdeb"
        if self.get("autoreqprov") == "yes":
            yield "+\tdh_shlibdeps"
        yield "+\tdh_gencontrol"
        yield "+\tdh_md5sums"
        yield "+\tdh_builddeb"
        yield "+"
        yield "+binary: binary-indep binary-arch"
        yield "+.PHONY: build clean binary-indep binary-arch binary install"

    def deb_script(self, section):
        script = self.packages["%{name}"].get(section, "")
        on_ifelse_if = re.compile(r"\s*if\s+.*$")
        on_ifelse_then = re.compile(r".*;\s*then\s*$")
        on_ifelse_else = re.compile(r"\s*else\s*$|.*;\s*else\s*$")
        on_ifelse_ends = re.compile(r"\s*fi\s*$|.*;\s*fi\s*$")
        ifelse = 0
        for lines in script:
            for line in lines.split("\n"):
                if line.startswith("%setup"):
                    continue
                for _ in range(10):
                    old = line
                    line = re.sub("[%][{][?]_with[^{}]*[}]", "", line)
                    line = re.sub("[%][{][!][?]_with[^{}]*[}]", "", line)
                    if old == line:
                        break
                line = line.replace("$RPM_OPT_FLAGS", "$(CFLAGS)")
                line = line.replace("%{?jobs:-j%jobs}", "")
                old = line
                for name in self.has_names():
                    if "$(" in self.get(name):
                        # debian_special expands
                        value = self.get(name)
                        line = re.sub(r"[%%][{]%s[}]" % name, value, line)
                        line = re.sub(r"[%%]%s\b" % name, value, line)
                    elif name.startswith("_"):
                        # rpm_macros expands
                        value = "${%s}" % name
                        line = re.sub(r"[%%][{]%s[}]" % name, value, line)
                        line = re.sub(r"[%%]%s\b" % name, value, line)
                    else:
                        value = self.expand("%"+name)
                        line = re.sub(r"[%%][{]%s[}]" % name, value, line)
                        line = re.sub(r"[%%]%s\b" % name, value, line)
                line = re.sub(r"[%][{][?]\w+[}]", '', line)
                if old != line:
                    _log.debug(" -%s", old)
                    _log.debug(" +%s", line)
                found = re.search(r"[%]\w+\b", line)
                if found:
                    here = found.group(0)
                    _log.warning("unexpanded '%s' found:\n %s", here, line)
                found = re.search(r"[%][{][!?]*\w+[:}]", line)
                if found:
                    here = found.group(0)
                    _log.warning("unexpanded '%s' found:\n %s", here, line)
                if line.strip() == "rm -rf $(CURDIR)/debian/tmp" and section != "%clean":
                    _log.warning(
                        "found rm -rf %%buildroot in section %s (should only be in %%clean)", section)
                # ifelse handling
                found_ifelse_if = on_ifelse_if.match(line)
                found_ifelse_then = on_ifelse_then.match(line)
                found_ifelse_else = on_ifelse_else.match(line)
                found_ifelse_ends = on_ifelse_ends.match(line)
                if found_ifelse_if and not found_ifelse_then:
                    _log.error(
                        "'if'-line without '; then' -> not supported\n %s", line)
                    ifelse += 1
                elif found_ifelse_then:
                    line = line + " \\"
                    ifelse += 1
                elif found_ifelse_else:
                    line = line + " \\"
                    if not ifelse:
                        _log.error("'else' outside ';then'-block")
                elif found_ifelse_ends:
                    ifelse += -1
                elif ifelse and not line.strip().endswith("\\"):
                    line += "; \\"
                if line.strip():
                    yield line

    def debian_scripts(self, nextfile=_nextfile):
        preinst = """
        if   [ "install" = "$1" ]; then  shift ; set -- "0" "$@"
        elif [ "update" = "$1" ]; then   shift ; set -- "1" "$@"
        fi
        """
        postinst = """
        if   [ "configure" = "$1" ] && [ "." = ".$2" ]; then  shift ; set -- "1" "$@"
        elif [ "configure" = "$1" ] && [ "." != ".$2" ]; then shift ; set -- "2" "$@"
        fi
        """
        prerm = """
        if   [ "remove" = "$1" ]; then  shift ; set -- "1" "$@"
        elif [ "upgrade" = "$1" ]; then shift ; set -- "2" "$@"
        fi
        """
        postrm = """
        if   [ "remove" = "$1" ]; then  shift ; set -- "0" "$@"
        elif [ "upgrade" = "$1" ]; then shift ; set -- "1" "$@"
        fi
        """
        mapped = {
            "preinst": preinst,
            "postinst": postinst,
            "prerm": prerm,
            "postrm": postrm,
        }

        sections = [("preinst", "%pre"), ("postinst", "%post"),
                    ("prerm", "%preun"), ("postrm", "%postun")]
        for deb_package, package in sorted(self.deb_packages2()):
            for deb_section, section in sections:
                scripts = self.packages[package].get(section, "")
                if scripts:
                    yield nextfile+"debian/%s.%s" % (deb_package, deb_section)
                    yield "+#!/bin/bash"
                    for line in mapped[deb_section].split("\n"):
                        if line.strip():
                            yield "+"+line.strip()
                    yield "+"
                    if not isinstance(scripts, list):
                        scripts = [scripts]
                    for script in scripts:
                        for line in script.split("\n"):
                            yield "+"+self.expand(line)

    def debian_patches(self, nextfile=_nextfile):
        patches = []
        patch = self.get("patch")
        if patch:
            patches.append(patch)
        for n in range(100):
            patch = self.get("patch%i" % n)
            if patch:
                patches.append(patch)
        if patches:
            yield nextfile+"debian/patches/series"
            for patch in patches:
                yield "+"+patch
            for patch in patches:
                yield nextfile+"debian/patches/"+patch
                for line in open(patch):
                    yield "+"+line
        else:
            _log.info("no patches -> no debian/patches/series")
        yield nextfile+"debian/source/format"
        yield "+"+self.source_format

    def get_patch_path(self, subdir, patch):
        if "3." in self.source_format or self.debtransform:
            return patch
        else:
            return "%s/%s" % (subdir, patch)

    def debian_diff(self):
        for deb in (self.debian_control, self.debian_copyright, self.debian_install,
                    self.debian_changelog, self.debian_patches, self.debian_rules,
                    self.debian_scripts):
            src = self.deb_src()
            old = src+".orig"
            patch = None
            lines = []
            for line in deb(_nextfile):
                if isinstance(line, tuple):
                    _log.fatal("?? %s %s", deb, line)
                    line = " ".join(line)
                if line.startswith(_nextfile):
                    if patch:
                        yield "--- %s" % self.get_patch_path(old, patch)
                        yield "+++ %s" % self.get_patch_path(src, patch)
                        yield "@@ -0,0 +1,%i @@" % (len(lines))
                        for plus in lines:
                            yield plus
                    lines = []
                    patch = line[len(_nextfile):]
                else:
                    lines += [line]
            # end of deb
            if lines:
                if patch:
                    yield "--- %s" % self.get_patch_path(old, patch)
                    yield "+++ %s" % self.get_patch_path(src, patch)
                    yield "@@ -0,0 +1,%i @@" % (len(lines))
                    for plus in lines:
                        yield plus
                else:
                    _log.error("have lines but no patch name: %s", deb)

    def write_debian_dsc(self, filename, into=None):
        filepath = os.path.join(into or "", filename)
        f = open(filepath, "w")
        try:
            count = 0
            for line in self.debian_dsc(into=into):
                if line.startswith(_nextfile):
                    continue
                f.write(line[1:]+"\n")
                count += 1
            return "written '%s' with %i lines" % (filepath, count)
        finally:
            f.close()
        return "ERROR", filename

    def write_debian_diff(self, filename, into=None):
        if filename.endswith(".tar.gz") or filename.endswith(".tgz"):
            return self.write_debian_tar(filename, into=into)
        filepath = os.path.join(into or "", filename)
        if filename.endswith(".gz"):
            f = gzip.open(filepath, "wb")
        else:
            f = open(filepath, "w")
        try:
            count = 0
            for line in self.debian_diff():
                f.write((line + "\n").encode('utf-8'))
                count += 1
            f.close()
            self.debian_file = filename
            return "written '%s' with %i lines" % (filepath, count)
        finally:
            f.close()
        return "ERROR: %s" % filepath

    def write_debian_tar(self, filename, into=None):
        if filename.endswith(".diff") or filename.endswith(".diff.gz"):
            return self.write_debian_diff(filename, into=into)
        filepath = os.path.join(into or "", filename)
        if filename.endswith(".gz"):
            tar = tarfile.open(filepath, "w:gz")
        else:
            tar = tarfile.open(filepath, "w:")
        try:
            state = None
            name = ""
            f = None
            for line in self.debian_diff():
                if line.startswith("--- "):
                    if name:
                        f.flush()
                        tar.add(f.name, name)
                        f.close()
                        name = ""
                    state = "---"
                    continue
                if line.startswith("+++ ") and state == "---":
                    name = line[len("+++ "):]
                    f = tempfile.NamedTemporaryFile()
                    state = "+++"
                    continue
                if line.startswith("@@") and state == "+++":
                    state = "+"
                    continue
                if line.startswith("+") and state == "+":
                    f.write(line[1:] + "\n")
                    continue
                _log.warning("unknown %s line:\n %s", state, line)
            if name:
                f.flush()
                tar.add(f.name, name)
                f.close()
                name = ""
            tar.close()
            self.debian_file = filename
            return "written '%s'" % filepath
        finally:
            tar.close()
        return "ERROR: %s" % filepath

    def write_debian_orig_tar(self, filename, into=None, path=None):
        sourcefile = self.expand(self.deb_sourcefile())
        if not os.path.isfile(sourcefile):
            sourcefile = os.path.join(path or "", sourcefile)
            print("----------------- sourcefile " + sourcefile)
        filepath = os.path.join(into or "", filename)
        if sourcefile.endswith(".tar.gz") or sourcefile.endswith(".tgz"):
            _log.info("copy %s to %s", sourcefile, filename)
            shutil.copyfile(sourcefile, filepath)
            self.source_orig_file = filename
            return "written '%s'" % filepath
        elif sourcefile.endswith(".tar.xz"):
            _log.info("recompress %s to %s", sourcefile, filename)
            gz = gzip.GzipFile(filepath, "w")
            gz.write(lzma.open(sourcefile).read())
            gz.close()
            self.source_orig_file = filename
            return "written '%s'" % filepath
        elif sourcefile.endswith(".tar.bz2"):
            _log.info("recompress %s to %s", sourcefile, filename)
            gz = gzip.GzipFile(filepath, "w")
            bz = bz2.BZ2File(sourcefile, "r")
            gz.write(bz.read())
            gz.close()
            bz.close()
            self.source_orig_file = filename
            return "written '%s'" % filepath
        elif sourcefile.endswith(".zip"):
            _log.info("recompress %s to %s", sourcefile, filename)
            # inspired by https://bitbucket.org/ruamel/zip2tar which is much more elaborate...
            with ZipFile(sourcefile) as zipf:
                with tarfile.open(filepath, "w:gz") as tarf:
                    for zip_info in zipf.infolist():
                        tar_info = tarfile.TarInfo(name=zip_info.filename)
                        tar_info.size = zip_info.file_size
                        # time.mktime takes 9 arguments; zip_info.date_time is only a 6-tuple: so we add 3 values.
                        tar_info.mtime = time.mktime(
                            zip_info.date_time + (-1, -1, -1))
                        # extract the file permissions from the zip_info.external_attr. Inspiration found here:
                        # https://stackoverflow.com/questions/434641/how-do-i-set-permissions-attributes-on-a-file-in-a-zip-file-using-pythons-zip
                        # the easy solution would have been to set permissions 755 for all files and directories...
                        # 0x10 is the MS-DOS directory flag; used to detect directories.
                        # in python 3.6 we could use zip_info.is_dir()
                        if zip_info.external_attr & 0x10 or \
                                zip_info.external_attr >> 16 & 0o755:  # seems like write permissions had been set
                            # directory or executable
                            tar_info.mode = 0o755
                        else:
                            # default: read-only
                            tar_info.mode = 0o644
                        tarf.addfile(
                            tarinfo=tar_info,
                            fileobj=zipf.open(zip_info.filename)
                        )
            self.source_orig_file = filename
            return "written '%s'" % filepath
        else:
            _log.error("unknown input source type: %s", sourcefile)
            _log.fatal("can not do a copy to %s", filename)


_hint = """NOTE: if neither -f nor -o is given (or any --debian-output) then
both of these two are generated from the last given *.spec argument file name."""
_o = OptionParser("%program [options] package.spec",
                  description=__doc__, epilog=_hint)
_o.add_option("-v", "--verbose", action="count",
              help="show more runtime messages", default=0)
_o.add_option("-0", "--quiet", action="count",
              help="show less runtime messages", default=0)
_o.add_option("-1", "--vars", action="count",
              help="show the variables after parsing")
_o.add_option("-2", "--packages", action="count",
              help="show the package settings after parsing")
_o.add_option("-x", "--extract", action="count",
              help="run dpkg-source -x after generation")
_o.add_option("-b", "--build", action="count",
              help="run dpkg-source -b after generation")
_o.add_option("--format", metavar=source_format,
              help="specify debian/source/format affecting generation")
_o.add_option("--debhelper", metavar=debhelper_compat,
              help="specify debian/compat debhelper level")
_o.add_option("--no-debtransform", action="count",
              help="disable dependency on OBS debtransform")
_o.add_option("--debtransform", action="count",
              help="enable dependency on OBS debtransform (%default)", default=debtransform)
_o.add_option("--urgency", metavar=urgency,
              help="set urgency level for debian/changelog")
_o.add_option("--promote", metavar=promote,
              help="set distribution level for debian/changelog")
_o.add_option("--importance", metavar=package_importance,
              help="set package priority for the debian/control file")
_o.add_option("-C", "--debian-control", action="count",
              help="output for the debian/control file")
_o.add_option("-L", "--debian-copyright", action="count",
              help="output for the debian/copyright file")
_o.add_option("-I", "--debian-install", action="count",
              help="output for the debian/*.install files")
_o.add_option("-S", "--debian-scripts", action="count",
              help="output for the postinst/prerm scripts")
_o.add_option("-H", "--debian-changelog", action="count",
              help="output for the debian/changelog file")
_o.add_option("-R", "--debian-rules", action="count",
              help="output for the debian/rules")
_o.add_option("-P", "--debian-patches", action="count",
              help="output for the debian/patches/*")
_o.add_option("-F", "--debian-diff", action="count",
              help="output for the debian.diff combined file")
_o.add_option("-D", "--debian-dsc", action="count",
              help="output for the debian *.dsc descriptor")
_o.add_option("-t", "--tar", metavar="FILE",
              help="create an orig.tar.gz copy of rpm Source0")
_o.add_option("-o", "--dsc", metavar="FILE",
              help="create the debian.dsc descriptor file")
_o.add_option("-f", "--diff", metavar="FILE", help="""create the debian.diff.gz file
(depending on the given filename it can also be a debian.tar.gz with the same content)""")
_o.add_option("--define", metavar="VARIABLE=VALUE", dest="defines",
              help="Specify a variable value in case spec parsing cannot determine it", action="append", default=[])
_o.add_option("-p", metavar="path", dest="path",
              help="Specify a path where to look for sources")
_o.add_option("-d", metavar="sources", help="""create and populate a debian sources
directory. Automatically sets --dsc and --diff, creates an orig.tar.gz and assumes --no-debtransform""")
_o.add_option("--nocheck", action="count", help="skip unit-tests")
_o.add_option("--nostrip", action="count",
              help="don't strip the files before packaging")


def main(args_in):
    opts, args = _o.parse_args(args_in)
    logging.basicConfig(format="%(levelname)s: %(message)s",
                        level=max(0, logging.INFO - 5 * (opts.verbose - opts.quiet)))
    DONE = logging.INFO + 5
    logging.addLevelName(DONE, "DONE")
    HINT = logging.INFO - 5
    logging.addLevelName(HINT, "HINT")
    work = RpmSpecToDebianControl()
    work.set_source_format(opts.format)
    spec = None
    if not args:
        specs = glob.glob("*.spec")
        if len(specs) == 1:
            args = specs
            _log.log(
                HINT, "no file arguments given but '%s' found to be the only *.spec here.", specs[0])
        elif len(specs) > 1:
            _o.print_help()
            _log.warning("")
            _log.warning(
                "no file arguments given and multiple *.spec files in the current directory:")
            _log.warning(" %s", specs)
            sys.exit(1)  # nothing was done
        else:
            _o.print_help()
            _log.warning("")
            _log.warning(
                "no file arguments given and no *.spec files in the current directory.")
            _log.warning("")
            sys.exit(1)  # nothing was done

    if opts.defines:
        for name, value in [valuepair.split('=', 1) for valuepair in opts.defines]:
            work.set(name, value, "define")

    for arg in args:
        work.parse(arg)
        if ".spec" in arg:
            spec = arg
    done = 0
    if opts.nocheck:
        work.check = False
    if opts.nostrip:
        work.strip = False
    if opts.importance:
        work.set_package_importance(opts.importance)
    if opts.debtransform:
        work.debtransform = True
    if opts.no_debtransform:
        work.debtransform = False
    if opts.debhelper:
        work.debhelper_compat = opts.debhelper
    if opts.urgency:
        work.urgency = opts.urgency
    if opts.promote:
        work.promote = opts.promote
    if opts.vars:
        done += opts.vars
        print("# have %s variables" % len(work.var))
        for name in sorted(work.has_names()):
            typed = work.typed[name]
            print("%%%s %s %s" % (typed, name, work.get(name)))
    else:
        _log.log(HINT, "have %s variables (use -1 to show them)" %
                 len(work.var))
    if opts.packages:
        done += opts.packages
        print("# have %s packages" % len(work.packages))
        for package in sorted(work.packages):
            print(" %package -n", package)
            for name in sorted(work.packages[package]):
                print("  %s:%s" % (name, work.packages[package][name]))
    else:
        _log.log(HINT, "have %s packages (use -2 to show them)" %
                 len(work.packages))
    if opts.debian_control:
        done += opts.debian_control
        for line in work.debian_control():
            print(line)
    if opts.debian_copyright:
        done += opts.debian_copyright
        for line in work.debian_copyright():
            print(line)
    if opts.debian_install:
        done += opts.debian_install
        for line in work.debian_install():
            print(line)
    if opts.debian_changelog:
        done += opts.debian_changelog
        for line in work.debian_changelog():
            print(line)
    if opts.debian_rules:
        done += opts.debian_rules
        for line in work.debian_rules():
            print(line)
    if opts.debian_patches:
        done += opts.debian_patches
        for line in work.debian_patches():
            print(line)
    if opts.debian_scripts:
        done += opts.debian_scripts
        for line in work.debian_scripts():
            print(line)
    if opts.debian_dsc:
        done += opts.debian_dsc
        for line in work.debian_dsc():
            print(line)
    if opts.debian_diff:
        done += opts.debian_diff
        for line in work.debian_diff():
            print(line)
    if opts.d:
        opts.d += "/"
        if not opts.dsc:
            opts.dsc = os.path.join(opts.d, os.path.basename(spec) + ".dsc")
        if not opts.diff:
            if "3." in work.source_format:
                opts.diff = "%s_%s.debian.tar.gz" % (
                    work.deb_source(), work.deb_revision())
            else:
                opts.diff = "%s_%s.diff.gz" % (
                    work.deb_source(), work.deb_revision())
        if not opts.tar:
            opts.tar = "%s_%s.orig.tar.gz" % (
                work.deb_source(), work.deb_version())
        work.debtransform = False
        if not os.path.isdir(opts.d):
            os.mkdir(opts.d)
    elif not done and not opts.diff and not opts.dsc:
        if work.debtransform:
            work.debian_file = "debian.tar.gz"
        elif "3." in work.source_format:
            work.debian_file = spec+".debian.tar.gz"
        else:
            work.debian_file = spec+".debian.diff.gz"
        opts.dsc = spec+".dsc"
        opts.diff = work.debian_file
        _log.log(HINT, "automatically selecting -o %s -f %s",
                 opts.dsc, opts.diff)
    if opts.tar:
        _log.log(DONE, work.write_debian_orig_tar(
            opts.tar, into=opts.d, path=opts.path))
    if opts.diff:
        _log.log(DONE, work.write_debian_diff(opts.diff, into=opts.d))
    if opts.dsc:
        _log.log(DONE, work.write_debian_dsc(opts.dsc, into=opts.d))
    _log.info("converted %s packages from %s", len(work.packages), args)
    if opts.extract:
        cmd = "cd %s && dpkg-source -x %s" % (opts.d or ".", opts.dsc)
        _log.log(HINT, cmd)
        output = subprocess.check_output(cmd, shell=True)
        _log.info("%s", output)
    if opts.build:
        cmd = "cd %s && dpkg-source -b %s" % (opts.d or ".", work.deb_src())
        _log.log(HINT, cmd)
        output = subprocess.check_output(cmd, shell=True)
        _log.info("%s", output)


if __name__ == "__main__":
    main(sys.argv[1:])
