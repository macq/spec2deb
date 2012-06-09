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

import re
import string
import os.path
import gzip
import tarfile
import tempfile
import logging
import commands
import glob
import sys

_log = logging.getLogger(__name__)
urgency = "low"
promote = "unstable"
standards_version = "3.8.2"

debtransform = False
if os.path.isdir(".osc"):
    debtransform = True

# NOTE: the OBS will enable DEB_TRANSFORM only if there is any file named 
#       debian.* in the sources area. Therefore the debian file must be 
#       named "debian.diff.gz" for OBS and NOT "package.debian.diff.gz".
#       (check https://github.com/openSUSE/obs-build/blob/master/build
#        and look for "DEB_TRANSFORM=true" and its if-condition)
# NOTE: debtransform itself is also right there:
#       https://github.com/openSUSE/obs-build/blob/master/debtransform
#       https://github.com/openSUSE/obs-build/blob/master/debtransformbz2
# HINT: in order to debug debtransform problems, just download the scripts,
#       and run them in your sources directory (*tar, *.spec,*.dsc) 
#       (rm -rf x; mkdir x; debtransform . *.dsc x/; cd x; dpkg-source -x *.dsc)

NEXT = "--- "
FORMAT = "1.0" # "2.0" # "3.0 (quilt)" # 

_formats = { 
    "1" : "1.0", 
    "1.0" : "1.0",
    "3" : "3.0 (quilt)", 
    "3.0" : "3.0 (quilt)", 
    "3.0 (quilt)" : "3.0 (quilt)"
}

usr_lib_rpm_macros = """# copy-n-paste from /usr/lib/rpm/macros
%_usr                   /usr
%_usrsrc                %{_usr}/src
%_var                   /var
%__cp                   /bin/cp
%__install              /usr/bin/install
%__ln_s                 ln -s
%__mkdir                /bin/mkdir
%__mkdir_p              /bin/mkdir -p
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
%_tmppath               %{_var}/tmp
"""

debian_special_macros = """
%__make                 $(MAKE)
%buildroot              $(CURDIR)/debian/tmp
%host                   $(DEB_HOST_GNU_TYPE)
%host_alias             $(DEB_HOST_GNU_TYPE)
%build                  $(DEB_BUILD_GNU_TYPE)
%build_alias            $(DEB_BUILD_GNU_TYPE)
"""

known_package_mapping = { 
    "zlib-dev" : "zlib1g-dev",
    "sdl-dev" : "libsdl-dev",
    "sdl" : "libsdl",
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
        self.var = {}
        self.typed = {}
        self.urgency = urgency
        self.promote = promote
        self.standards_version = standards_version
        self.format = FORMAT
        self.debtransform = debtransform
        self.scan_macros(usr_lib_rpm_macros, "default")
        self.scan_macros(debian_special_macros, "debian")
    def has_names(self):
        return self.var.keys()
    def has(self, name):
        if name in self.var:
            return True
        return False
    def get(self, name, default = None):
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
    def set_format(self, value):
        if not value:
            pass
        elif value in _formats:
            self.format = _formats[value]
            _log.info("using source format '%s'" % self.format)
        else:
            _log.fatal("unknown source format: '%s'" % value)
    def new_state(self, state):
        if not self.states:
            self.states = [ "" ]
        self.states[0] = state
    on_explicit_package = re.compile(r"-n\s+(\S+)")
    def new_package(self, package, options):
        package = package or ""
        options = options or ""
        found = self.on_explicit_package.search(options)
        if found:
            self.package = found.group(0)
        else:
            name = package.strip()
            if name:
                self.package = "%{name}-"+name
            else:
                self.package = "%{name}"
        self.packages.setdefault(self.package, {})
    def append_setting(self, name, value):
        self.packages[self.package].setdefault(name,[]).append(value.strip())
        # also provide the setting for macro expansion:
        ignores =  [ "requires","buildrequires","prereq",
                     "provides", "conflicts", "suggests"]
        if not self.package or self.package == "%{name}":
            if not name.startswith("%"):
                name1 = string.lower(name)
                name2 = string.upper(name)
                if name1 in ["source", "patch"]:
                    name1 += "0"
                    name2 += "0"
                if name1 not in ignores:
                    self.set(name1, value.strip(), "package")
                    self.set(name2, value.strip(), "package")
            else:
                _log.debug("ignored to add a setting '%s'", name)
        else:
            if name not in ignores:
                _log.debug("ignored to add a setting '%s' from package '%s'", 
                           name, self.package)
    def new_section(self, section, text = ""):
        self.section = section.strip()
        self.sectiontext = text
    def append_section(self, text = None):
        self.sectiontext += text or ""
    on_variable = re.compile(r"%(define|global)\s+(\S+)\s+(.*)")
    def save_variable(self, found_variable):
        typed, name, value = found_variable.groups()
        self.set(name.strip(), value.strip(), typed)
    on_setting = re.compile(r"\s*(\w+)\s*:\s*(\S.*)")
    def save_setting(self, found_setting):
        name, value = found_setting.groups()
        self.append_setting(string.lower(name), value)
    on_new_if = re.compile(r"%if\b(.*)")
    on_end_if = re.compile(r"%endif\b(.*)")
    def new_if(self, found_new_if):
        condition, = found_new_if.groups()
        if "debian" in condition:
            self.states.append("keep-if")
        else:
            self.states.append("skip-if")
    def end_if(self, found_new_if):
        if self.states[-1] == "skip-if":
            self.states = self.states[:-1]
        elif self.states[-1] == "keep-if":
            self.states = self.states[:-1]
        else:
            _log.error("unmatched %endif with no preceding %if")
    def skip_if(self):
        if "skip-if" in self.states:
            return True
        return False
    on_default_var1 = re.compile(r"\s*%\{!\?(\w+):\s+%(define|global)\s+\1\b(.*)\}")
    def default_var1(self, found_default_var):
        name, typed, value = found_default_var.groups()
        if not self.has(name):
            self.set(name.strip(), value.strip(), typed)
        else:
            _log.debug("override %%%s %s %s", typed, name, value)
        if typed != "global":
            _log.warning("do not use %%define in default-variables, use %%global %s", name) 
    on_default_var2 = re.compile(r"\s*[%][{][!][?](\w+)[:]\s*[%][{][?](\w+)[:]\s*[%](define|global)\s+\1\b(.*)[}][}]")
    def default_var2(self, found_default_var):
        name, name2, typed, value = found_default_var.groups()
        if not self.has(name2):
            return
        if not self.has(name):
            self.set(name, value.strip(), typed)
        else:
            _log.debug("override %%%s %s %s", typed, name, value)
        if typed != "global":
            _log.warning("do not use %%define in default-variables, use %%global %s", name) 
    on_default_var3 = re.compile(r"\s*[%][{][!][?](\w+)[:]\s*[%][{][?](\w+)[:]\s*[%](define|global)\s+\1\b(.*)[}][}]")
    def default_var3(self, found_default_var):
        name, name3, typed, value = found_default_var.groups()
        if self.has(name3):
            return
        if not self.has(name):
            self.set(name, value.strip(), typed)
        else:
            _log.debug("override %%%s %s %s", typed, name, value)
        if typed != "global":
            _log.warning("do not use %%define in default-variables, use %%global %s", name) 
    on_package = re.compile(r"%(package)(?:\s+(\S+))?(?:\s+(-.*))?")
    def start_package(self, found_package):
        rule, package, options = found_package.groups()
        self.new_package(package, options)
        self.new_state("package")
    on_description = re.compile(r"%(description)(?:\s+(\S+))?(?:\s+(-.*))?")
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
    on_scripts = re.compile(r"%(post|postun|pre|preun)\b(?:\s+(\w\S+))?(?:\s+(-.*))?")
    def start_scripts(self, found_scripts):
        rule, package, options = found_scripts.groups()
        self.new_package(package, options)
        self.new_section("%"+rule.strip())
        self.new_state("scripts")
    def endof_scripts(self):
        self.append_setting(self.section, self.sectiontext)
    on_files = re.compile(r"%(files)(?:\s+(\S+))?(?:\s+(-.*))?")
    def start_files(self, found_files):
        rule, package, options = found_files.groups()
        self.new_package(package, options)
        self.new_section("%"+rule)
        self.new_state("files")
    def endof_files(self):
        self.append_setting(self.section, self.sectiontext)
    def parse(self, rpmspec):
        default = "%package "
        found_package = self.on_package.match(default)
        assert found_package
        self.start_package(found_package)
        for line in open(rpmspec):
            if self.state() in [ "package" ]:
                found_default_var1 = self.on_default_var1.match(line)
                found_default_var2 = self.on_default_var2.match(line)
                found_new_if = self.on_new_if.match(line)
                found_end_if = self.on_end_if.match(line)
                found_comment = self.on_comment.match(line)
                found_variable = self.on_variable.match(line)
                found_setting = self.on_setting.match(line)
                found_package = self.on_package.match(line)
                found_description = self.on_description.match(line)
                found_rules = self.on_rules.match(line)
                found_scripts = self.on_scripts.match(line)
                found_files = self.on_files.match(line)
                found_changelog = self.on_changelog.match(line)
                if found_comment:
                    pass
                elif found_default_var1:
                    self.default_var1(found_default_var1)
                elif found_default_var2:
                    self.default_var2(found_default_var2)
                elif found_new_if:
                    self.new_if(found_new_if)
                elif found_end_if:
                    self.end_if(found_end_if)
                elif self.skip_if():
                    continue
                elif found_variable:
                    self.save_variable(found_variable)
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
                elif not line.strip():
                    pass
                else:
                    _log.error("%s unmatched line:\n %s", self.state(), line)
            elif self.state() in [ "description"]:
                found_package = self.on_package.match(line)
                found_description = self.on_description.match(line)
                found_rules = self.on_rules.match(line)
                found_scripts = self.on_scripts.match(line)
                found_files = self.on_files.match(line)
                found_changelog = self.on_changelog.match(line)
                if (found_package or found_description or found_rules or found_scripts 
                        or found_files or found_changelog):
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
                else:
                    self.append_section(line)
            elif self.state() in [ "rules" ]:
                found_package = self.on_package.match(line)
                found_description = self.on_description.match(line)
                found_rules = self.on_rules.match(line)
                found_scripts = self.on_scripts.match(line)
                found_files = self.on_files.match(line)
                found_changelog = self.on_changelog.match(line)
                if (found_package or found_description or found_rules or found_scripts 
                        or found_files or found_changelog):
                    self.endof_files()
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
                else:
                    self.append_section(line)
            elif self.state() in [ "scripts" ]:
                found_package = self.on_package.match(line)
                found_description = self.on_description.match(line)
                found_rules = self.on_rules.match(line)
                found_scripts = self.on_scripts.match(line)
                found_files = self.on_files.match(line)
                found_changelog = self.on_changelog.match(line)
                if (found_package or found_description or found_rules or found_scripts 
                        or found_files or found_changelog):
                    self.endof_scripts()
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
                else:
                    self.append_section(line)
            elif self.state() in [ "files" ]:
                found_package = self.on_package.match(line)
                found_description = self.on_description.match(line)
                found_rules = self.on_rules.match(line)
                found_scripts = self.on_scripts.match(line)
                found_files = self.on_files.match(line)
                found_changelog = self.on_changelog.match(line)
                if (found_package or found_description or found_rules or found_scripts 
                        or found_files or found_changelog):
                    self.endof_files()
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
                else:
                    self.append_section(line)
            elif self.state() in [ "changelog"]:
                found_package = self.on_package.match(line)
                found_description = self.on_description.match(line)
                found_rules = self.on_rules.match(line)
                found_scripts = self.on_scripts.match(line)
                found_files = self.on_files.match(line)
                found_changelog = self.on_changelog.match(line)
                if (found_package or found_description or found_rules or found_scripts 
                        or found_files or found_changelog):
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
                else:
                    self.append_section(line)
            else:
                _log.fatal("UNKNOWN state %s", self.states)
        # for line
        if self.state() in [ "package"]:
            pass
        elif self.state() in [ "description" ]:
            self.endof_description()
        elif self.state() in [ "rules" ]:
            self.endof_description()
        elif self.state() in [ "scripts" ]:
            self.endof_scripts()
        elif self.state() in [ "files" ]:
            self.endof_files()
        elif self.state() in [ "changelog" ]:
            self.endof_changelog()
        else:
            _log.fatal("UNKNOWN state %s (at end of file)", self.states)
    def package_mapping(self, package):
        known = known_package_mapping
        if package.endswith("-devel"):
            package = package[:-2]
        if package in known:
            package = known[package]
        return package
    on_var1 = re.compile(r"%(\w+)\b")
    on_var2 = re.compile(r"%{(\w+)}")
    def expand(self, text):
        orig = text
        on_plain_name = re.compile(r"[%](\w+)\b")
        on_required_name = re.compile(r"[%][{](\w+)[}]")
        on_optional_name = re.compile(r"[%][{][?](\w+)[}]")
        for _ in xrange(100):
            oldtext = text
            for found in on_plain_name.finditer(text):
                name, = found.groups()
                if self.has(name):
                    value = self.get(name)
                    text = re.sub("%"+name+"\\b", value, text)
                else:
                    _log.error("unable to expand %%%s in:\n %s", name, orig)
            for found in on_required_name.finditer(text):
                name, = found.groups()
                if self.has(name):
                    value = self.get(name)
                    text = re.sub("%{"+name+"}", value , text)
                else:
                    _log.error("unable to expand %%{%s} in:\n %s", name, orig)
            for found in on_optional_name.finditer(text):
                name, = found.groups()
                if self.has(name):
                    value = ''
                    text = re.sub("%{?"+name+"}", value, text)
                else:
                    _log.debug("expand optional %%{?%s} to '' in: '%s'", name, orig)
            if oldtext == text:
                break
        if "$(" in text and orig not in [ "%buildroot", "%__make" ]:
            _log.warning("expand of '%s' left a make variable:\n %s", orig, text)
        return text
    def deb_packages(self):
        for deb, pkg in self.deb_packages2():
            yield deb
    def deb_packages2(self):
        for package in sorted(self.packages):
            deb_package = package
            if deb_package == "%{name}" and len(self.packages) > 1:
                deb_package = "%{name}-bin"
            yield self.package_mapping(self.expand(deb_package)), package
    def deb_build_depends(self):
        depends = [ "debhelper (>= 7)" ]
        for package in self.packages:
            for buildrequires in self.packages[package].get("buildrequires", []):
                depend = self.deb_requires(buildrequires)
                if depend not in depends:
                    depends.append(depend)
        return depends
    def deb_requires(self, requires):
        withversion = re.match("(\S+)\s+(>=|>|<|<=|==)\s+(\S+)", requires)
        if withversion:
            package, relation, version = withversion.groups()
            depend = "%s (%s %s)" % (string.lower(package), relation, version)
        else:
            depend = string.lower(requires.strip())
        return self.package_mapping(depend)
    def deb_sourcefile(self):
        sourcefile = self.get("source", self.get("source0"))
        x = sourcefile.rfind("/")
        if x:
            sourcefile = sourcefile[x+1:]
        return sourcefile
    def deb_version(self):
        return self.get("version","")
    def deb_source(self, sourcefile = None):
        return self.get("name")
    def deb_src(self):
        script = self.packages["%{name}"].get("%prep", "")
        for part in script:
            for line in part.split("\n"):
                if line.startswith("%setup"):
                    m = re.search("-n\s+(    \S+)", line)
                    if m:
                        return m.group(1)
                    return self.deb_source()+"-"+self.deb_version()
        _log.error("no %setup in %prep section found")
    def debian_dsc(self, next = NEXT, into = None):
        yield next+"debian/dsc"
        yield "+Format: %s" % self.format
        sourcefile = self.deb_sourcefile()
        source = self.deb_source(sourcefile)
        yield "+Source: %s" % self.expand(source)
        binaries = list(self.deb_packages())
        yield "+Binary: %s" % ", ".join(binaries)
        yield "+Architecture: %s" % "any"
        version = self.get("version","0")+"-"+self.get("revision","0")
        yield "+Version: %s" % version
        yield "+Maintainer: %s" % self.get("packager","?")
        yield "+Standards-Version: %s" % self.standards_version
        yield "+Homepage: %s" % self.get("url","")
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
                _log.debug("source_orig '%s' size %s", source_orig_path, source_orig_size)
            else:
                source_orig_size = 0
                _log.info("source_orig '%s' not found", source_orig_path)
            if os.path.exists(debian_file_path):
                debian_file_size = os.path.getsize(debian_file_path)
                _log.debug("debian_file '%s' size %s", debian_file_path, debian_file_size)
            else:
                debian_file_size = 0
                _log.info("debian_file '%s' not found", debian_file_path)
            yield "+Files: %s" % ""
            yield "+ %s %i %s" %( source_orig_md5sum, source_orig_size, source_orig)
            yield "+ %s %s %s" %( debian_file_md5sum, debian_file_size, debian_file)
    def md5sum(self, filename):
        if not os.path.exists(filename):
            return "0" * 32
        import hashlib
        md5 = hashlib.md5() #@UndefinedVariable
        md5.update(open(filename).read())
        return md5.hexdigest()
    def group2section(self, group):
        if isinstance(group, list) and len(group) >= 1:
            group = group[0]
        if "Lib" in group:
            return "libs"
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
    def debian_control(self, next = NEXT):
        yield next+"debian/control"
        group = self.get("group","System/Libraries")
        section = self.group2section(group)
        yield "+Priority: %s" % "optional"
        yield "+Maintainer: %s" % self.get("packager","?")
        source = self.deb_source()
        yield "+Source: %s" % self.expand(source)
        depends = list(self.deb_build_depends())
        yield "+Build-Depends: %s" % ", ".join(depends)
        yield "+Standards-Version: %s" % self.standards_version
        yield "+Homepage: %s" % self.get("url","")
        yield "+"
        for deb_package, package in sorted(self.deb_packages2()):
            yield "+Package: %s" % deb_package
            group = self.packages[package].get("group", "System/Libraries")
            section = self.group2section(group)
            yield "+Section: %s" % section
            yield "+Architecture: %s" % "any"
            depends = self.packages[package].get("depends", "")
            replaces = self.packages[package].get("replaces", "")
            conflicts = self.packages[package].get("conflicts", "")
            pre_depends = self.packages[package].get("prereq", "")
            if depends:
                deb_depends = [self.deb_requires(req) for req in depends]
                yield "+Depends: %s" % ", ".join(deb_depends)
            if replaces:
                deb_replaces = [self.deb_requires(req) for req in replaces]
                yield "+Replaces: %s" % ", ".join(deb_replaces)
            if conflicts:
                deb_conflicts = [self.deb_requires(req) for req in conflicts]
                yield "+Conflicts: %s" % ", ".join(deb_conflicts)
            if pre_depends:
                deb_pre_depends = [self.deb_requires(req) for req in pre_depends]
                yield "+Pre-Depends: %s" % ", ".join(deb_pre_depends)
            text = self.packages[package].get("%description", "")
            # yield "+Description: %s" % self.deb_description_from(text)
            for line in self.deb_description_lines(text):
                yield "+"+line
            yield "+"
    def debian_copyright(self, next = NEXT):
        yield next+"debian/copyright"
        yield "+License: %s" % self.get("license","")
    def debian_install(self, next = NEXT):
        docs = []
        for deb_package, package in sorted(self.deb_packages2()):
            files_name =  "debian/%s.install" % deb_package
            dirs_name =  "debian/%s.dirs" % deb_package
            files_list = []
            dirs_list = []
            filesection = self.packages[package].get("%files", [""])
            if not isinstance(filesection, list): 
                filesection = [ filesection ]
            for files in filesection:
                for path in files.split("\n"):
                    if path.startswith("%config"):
                        path = path[len("%config"):].strip()
                        if path:
                            files_list.append(path)
                    elif path.startswith("%doc"):
                        path = path[len("%doc"):].strip()
                        docs += [ path ]
                        continue
                    elif path.startswith("%dir"):
                        path = path[len("%dir"):].strip()
                        if path:
                            dirs_list.append(path)
                        continue
                    elif path.startswith("%defattr"):
                        continue
                    else:
                        path = path.strip()
                        if path.startswith("/"):
                            path = path[1:]
                        if path:
                            files_list.append(path)
                        continue
            if dirs_list:
                yield next+dirs_name
                for path in dirs_list:
                    path = self.expand(path)
                    if path.startswith("/"):
                        path = path[1:]
                    yield "+"+path
            if files_list:
                yield next+files_name
                for path in files_list:
                    path = self.expand(path)
                    if path.startswith("/"):
                        path = path[1:]
                    yield "+"+path
        if docs:
            yield next+"docs"
            for path in docs:
                if True:
                    path = self.expand(path)
                    if path.startswith("/"):
                        path = path[1:]
                    yield "+"+path
    def debian_changelog(self, next = NEXT):
        name = self.expand(self.get("name"))
        version = self.expand(self.get("version"))
        packager = self.expand(self.get("packager"))
        promote = self.promote
        urgency = self.urgency
        yield next+"debian/changelog"
        yield "+%s (%s) %s; urgency=%s" % (name, version, promote, urgency)
        yield "+"
        yield "+  * generated OBS deb build"
        yield "+"
        yield "+ -- %s  Mon, 25 Dec 2007 10:50:38 +0100" % (packager)
    def debian_rules(self, next = NEXT):
        yield next+"debian/rules"
        yield "+#!/usr/bin/make -f"
        yield "+# -*- makefile -*-"
        yield "+# Uncomment this to turn on verbose mode."
        yield "+export DH_VERBOSE=1"
        yield "+"
        yield "+# These are used for cross-compiling and for saving the configure script"
        yield "+# from having to guess our platform (since we know it already)"
        yield "+DEB_HOST_GNU_TYPE   ?= $(shell dpkg-architecture -qDEB_HOST_GNU_TYPE)"
        yield "+DEB_BUILD_GNU_TYPE  ?= $(shell dpkg-architecture -qDEB_BUILD_GNU_TYPE)"
        yield "+"
        yield "+"
        yield "+CFLAGS = -Wall -g"
        yield "+"
        yield "+ifneq (,$(findstring noopt,$(DEB_BUILD_OPTIONS)))"
        yield "+       CFLAGS += -O0"
        yield "+else"
        yield "+       CFLAGS += -O2"
        yield "+endif"
        yield "+ifeq (,$(findstring nostrip,$(DEB_BUILD_OPTIONS)))"
        yield "+       INSTALL_PROGRAM += -s"
        yield "+endif"
        yield "+"
        for name in self.has_names():
            if name.startswith("_"):
                value = self.get(name)
                value2 = re.sub(r"[%][{](\w+)[}]", r"$(\1)", value)
                yield "+%s=%s" % (name, value2)
        yield "+"
        yield "+configure: configure-stamp"
        yield "+configure-stamp:"
        yield "+\tdh_testdir"
        for line in self.deb_script("%prep"):
            yield "+\t"+line
        yield "+\t#"
        yield "+\ttouch configure-stamp"
        yield "+"
        yield "+build: build-stamp"
        yield "+build-stamp: configure-stamp"
        yield "+\tdh_testdir"
        for line in self.deb_script("%build"):
            yield "+\t"+line
        yield "+\t#"
        yield "+\ttouch build-stamp"
        yield "+"
        yield "+clean:"
        yield "+\tdh_testdir"
        yield "+\tdh_testroot"
        yield "+\trm -f configure-stamp build-stamp"
        yield "+\t[ ! -f Makefile ] || $(MAKE) distclean"
        yield "+\tdh_clean"
        yield "+"
        yield "+install: build"
        yield "+\tdh_testdir"
        yield "+\tdh_testroot"
        yield "+\tdh_prep"
        yield "+\tdh_installdirs"
        yield "+\t# Add here commands to install the package into debian/tmp"
        # +       $(MAKE) install DESTDIR=$(CURDIR)/debian/tmp
        for line in self.deb_script("%install"):
            yield "+\t"+line
        yield "+\t# Move all files in their corresponding package"
        yield "+\tdh_install --list-missing -s --sourcedir=debian/tmp"
        yield "+\t# empty dependency_libs in .la files"
        yield "+\tsed -i \"/dependency_libs/ s/'.*'/''/\" `find debian/ -name '*.la'`"
        yield "+"
        yield "+# Build architecture-independent files here."
        yield "+binary-indep: build install"
        yield "+# We have nothing to do by default."
        yield "+"
        yield "+# Build architecture-dependent files here."
        yield "+binary-arch: build install"
        yield "+\tdh_testdir"
        yield "+\tdh_testroot"
        yield "+\tdh_installchangelogs ChangeLog"
        yield "+\tdh_installdocs"
        yield "+\tdh_installexamples"
        yield "+\tdh_installman"
        yield "+\tdh_link"
        yield "+\tdh_strip"
        yield "+\tdh_compress"
        yield "+\tdh_fixperms"
        yield "+\tdh_makeshlibs -V"
        yield "+\tdh_installdeb"
        yield "+\tdh_shlibdeps"
        yield "+\tdh_gencontrol"
        yield "+\tdh_md5sums"
        yield "+\tdh_builddeb"
        yield "+"
        yield "+binary: binary-indep binary-arch"
        yield "+.PHONY: build clean binary-indep binary-arch binary install"
    def deb_script(self, section):
        script = self.packages["%{name}"].get(section, "")
        for lines in script:
            for line in lines.split("\n"):
                if line.startswith("%setup"): 
                    continue
                for _ in xrange(10):
                    old = line
                    line = re.sub("[%][{][?]_with[^{}]*[}]", "", line)
                    line = re.sub("[%][{][!][?]_with[^{}]*[}]", "", line)
                    if old == line:
                        break
                # line = line.replace("%buildroot", "$(CURDIR)/debian/tmp")
                # line = line.replace("%{buildroot}", "$(CURDIR)/debian/tmp")
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
                        value = "$(%s)" % name
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
                if line.strip() == "rm -rf $(CURDIR)/debian/tmp":
                    if section != "%clean":
                        _log.warning("found rm -rf %%buildroot in section %s (should only be in %%clean)", section)
                if line.strip():
                    yield line
    def debian_scripts(self, next = NEXT):
        preinst = """
        if   [ "install" = "$1" ]; then  shift ; set -- "0" "$@"
        elif [ "update" = "$1" ]; then   shift ; set -- "1" "$@"
        fi
        """
        postinst = """
        if   [ "configure" = "$1" && "." = ".$2" ]; then  shift ; set -- "1" "$@"
        elif [ "configure" = "$1" && "." != ".$2" ]; then shift ; set -- "2" "$@"
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
                  "preinst" : preinst,
                  "postinst" : postinst,
                  "prerm" : prerm,
                  "postrm" : postrm,
                  }
        
        sections = [("preinst", "%pre"), ("postinst","%post"),
                    ("prerm", "%preun"), ("postrm","%postun")] 
        for deb_package, package in sorted(self.deb_packages2()):
            for deb_section, section in sections:
                scripts = self.packages["%{name}"].get(section, "")
                if scripts:
                    yield next+"%s.%s" %(deb_package, deb_section)
                    yield "+#! /bin/sh"
                    for line in mapped[deb_section].split("\n"):
                        if line.strip():
                            yield "+"+line.strip()
                    yield "+" 
                    if not isinstance(scripts, list):
                        scripts = [ scripts ]
                    for script in scripts:
                        for line in script.split("\n"):
                            yield "+"+self.expand(line)
    def debian_patches(self, next = NEXT):
        patches = []
        for n in xrange(1,100):
            source = self.get("source%i" % n)
            if source:
                try:
                    _log.debug("append source%i '%s' as a patch", n, source)
                    textfile = open(source)
                    yield next+source
                    for line in textfile:
                        yield "+"+line
                    textfile.close()
                    patches.append(source)
                except Exception, e:
                    _log.error("append source%i '%s' failed:\n %s", n, source, e)
        patch = self.get("patch")
        if patch:
            patches.append(patch)
        for n in xrange(100):
            patch = self.get("patch%i" % n)
            if patch:
                patches.append(patch)
        if patches:
            yield next+"debian/patches/series"
            for patch in patches:
                yield "+"+patch
            for patch in patches:
                yield next+"debian/patches/"+patch
                for line in open(patch):
                    yield "+"+line
        else:
            _log.info("no patches -> no debian/patches/series")
        yield next+"debian/source/format"
        yield "+"+self.format
    def p(self, subdir, patch):
        if "3." in self.format or self.debtransform:
            return patch
        else:
            return "%s/%s" % (subdir, patch)
    def debian_diff(self):
        for deb in (self.debian_control, self.debian_copyright, self.debian_install,
                    self.debian_changelog, self.debian_rules, self.debian_patches,
                    self.debian_scripts):
            src = self.deb_src()
            old = src+".orig"
            patch = None
            lines = []
            for line in deb(NEXT):
                if isinstance(line, tuple):
                    _log.fatal("?? %s %s", deb, line)
                    line = " ".join(line)
                if line.startswith(NEXT):
                    if patch:
                        yield "--- %s" % self.p(old, patch)
                        yield "+++ %s" % self.p(src, patch)
                        yield "@@ -0,0 +1,%i @@" % (len(lines))
                        for plus in lines:
                            yield plus
                    lines = []
                    patch = line[len(NEXT):]
                else:
                    lines += [ line ]
            # end of deb
            if True:
                if lines:
                    if patch:
                        yield "--- %s" % self.p(old, patch)
                        yield "+++ %s" % self.p(src, patch)
                        yield "@@ -0,0 +1,%i @@" % (len(lines))
                        for plus in lines:
                            yield plus
                    else:
                        _log.error("have lines but no patch name: %s", deb)
    def write_debian_dsc(self, filename, into = None):
        filepath = os.path.join(into or "", filename) 
        f = open(filepath, "w")
        try:
            count = 0
            for line in self.debian_dsc(into = into):
                if line.startswith(NEXT):
                    continue
                f.write(line[1:]+"\n")
                count +=1
            return "written '%s' with %i lines" % (filepath, count)
        finally:
            f.close()
        return "ERROR", filename
    def write_debian_diff(self, filename, into = None):
        if filename.endswith(".tar.gz"):
            return self.write_debian_tar(filename, into = into)
        filepath = os.path.join(into or "", filename) 
        if filename.endswith(".gz"):
            f = gzip.open(filepath, "w")
        else:
            f = open(filepath, "w")
        try:
            count = 0
            for line in self.debian_diff():
                f.write(line+"\n")
                count += 1
            f.close()
            self.debian_file = filename
            return "written '%s' with %i lines" % (filepath, count)
        finally:
            f.close()
        return "ERROR: %s" % filepath
    def write_debian_tar(self, filename, into = None):
        if filename.endswith(".diff") or filename.endswith(".diff.gz"):
            return self.write_debian_diff(filename, into = into)
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
            if True:
                if True:
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
    def write_debian_orig_tar(self, filename, into = None):
        sourcefile = self.expand(self.deb_sourcefile())
        filepath = os.path.join(into or "", filename) 
        if sourcefile.endswith(".tar.gz"):
            _log.info("copy %s to %s", sourcefile, filename)
            import shutil
            shutil.copyfile(sourcefile, filepath)
            self.source_orig_file = filename
            return "written '%s'" % filepath
        elif sourcefile.endswith(".tar.bz2"):
            _log.info("recompress %s to %s", sourcefile, filename)
            import bz2
            import gzip
            gz = gzip.GzipFile(filepath, "w")
            bz = bz2.BZ2File(sourcefile, "r")
            gz.write(bz.read())
            gz.close()
            bz.close()
            self.source_orig_file = filename
            return "written '%s'" % filepath
        else:
            _log.error("unknown input source type: %s", sourcefile)
            _log.fatal("can not do a copy to %s", filename)

from optparse import OptionParser
_hint = """NOTE: if neither -f nor -o is given (or any --debian-output) then 
both of these two are generated from the last given *.spec argument file name.""" 
_o = OptionParser("%program [options] package.spec", description = __doc__, epilog = _hint)
_o.add_option("-v","--verbose", action="count", help="show more runtime messages", default=0)
_o.add_option("-0","--quiet", action="count", help="show less runtime messages", default=0)
_o.add_option("-1","--vars",action="count", help="show the variables after parsing")
_o.add_option("-2","--packages",action="count", help="show the package settings after parsing")
_o.add_option("-x","--extract", action="count", help="run dpkg-source -x after generation")
_o.add_option("-b","--build", action="count", help="run dpkg-source -b after generation")
_o.add_option("--format",metavar=FORMAT, help="specify debian/source/format affecting generation")
_o.add_option("--no-debtransform",action="count", help="disable dependency on OBS debtransform")
_o.add_option("--debtransform",action="count", help="enable dependency on OBS debtransform (%default)", default = debtransform)
_o.add_option("--urgency", metavar=urgency, help="set urgency level for debian/changelog")
_o.add_option("--promote", metavar=promote, help="set distribution level for debian/changelog")
_o.add_option("-C","--debian-control",action="count", help="output for the debian/control file")
_o.add_option("-L","--debian-copyright",action="count", help="output for the debian/copyright file")
_o.add_option("-I","--debian-install",action="count", help="output for the debian/*.install files")
_o.add_option("-S","--debian-scripts",action="count", help="output for the postinst/prerm scripts")
_o.add_option("-H","--debian-changelog",action="count", help="output for the debian/changelog file")
_o.add_option("-R","--debian-rules",action="count", help="output for the debian/rules")
_o.add_option("-P","--debian-patches",action="count", help="output for the debian/patches/*")
_o.add_option("-F","--debian-diff",action="count", help="output for the debian.diff combined file")
_o.add_option("-D","--debian-dsc",action="count", help="output for the debian *.dsc descriptor")
_o.add_option("-t","--tar",metavar="FILE", help="create an orig.tar.gz copy of rpm Source0")
_o.add_option("-o","--dsc",metavar="FILE", help="create the debian.dsc descriptor file")
_o.add_option("-f","--diff",metavar="FILE", help="""create the debian.diff.gz file 
(depending on the given filename it can also be a debian.tar.gz with the same content)""")
_o.add_option("-d", metavar="sources", help="""create and populate a debian sources
directory. Automatically sets --dsc and --diff, creates an orig.tar.gz and assumes --no-debtransform""")

if __name__ == "__main__":
    opts, args = _o.parse_args()
    logging.basicConfig(format = "%(levelname)s: %(message)s",
                        level = max(0, logging.INFO - 5 * (opts.verbose - opts.quiet)))
    DONE = logging.INFO + 5; logging.addLevelName(DONE, "DONE")
    HINT = logging.INFO - 5; logging.addLevelName(HINT, "HINT")
    work = RpmSpecToDebianControl()
    work.set_format(opts.format)
    spec = None
    if not args:
        specs = glob.glob("*.spec")
        if len(specs) == 1:
            args = specs
            _log.log(HINT, "no file arguments given but '%s' found to be the only *.spec here.", specs[0])
        elif len(specs) > 1:
            _o.print_help()
            _log.warning("")
            _log.warning("no file arguments given and multiple *.spec files in the current directory:")
            _log.warning(" %s", specs)
            sys.exit(1) # nothing was done
        else:
            _o.print_help()
            _log.warning("")
            _log.warning("no file arguments given and no *.spec files in the current directory.")
            _log.warning("")
            sys.exit(1) # nothing was done
    for arg in args:
        work.parse(arg)
        if arg.endswith(".spec"):
            spec = arg[:-(len(".spec"))]
    done = 0
    if opts.debtransform:
        work.debtransform = True
    if opts.no_debtransform:
        work.debtransform = False
    if opts.urgency:
        work.urgency = opts.urgency
    if opts.promote:
        work.promote = opts.promote 
    if opts.vars:
        done += opts.vars
        print "# have %s variables" % len(work.var)
        for name in sorted(work.has_names()):
            typed = work.typed[name]
            print "%%%s %s %s" % (typed, name, work.get(name))
    else:
        _log.log(HINT, "have %s variables (use -1 to show them)" % len(work.var))
    if opts.packages:
        done += opts.packages
        print "# have %s packages" % len(work.packages)
        for package in sorted(work.packages):
            print " %package -n", package
            for name in sorted(work.packages[package]):
                print "  %s:%s" %(name, work.packages[package][name])
    else:
        _log.log(HINT, "have %s packages (use -2 to show them)" % len(work.packages))
    if opts.debian_control:
        done += opts.debian_control
        for line in work.debian_control():
            print line
    if opts.debian_copyright:
        done += opts.debian_copyright
        for line in work.debian_copyright():
            print line
    if opts.debian_install:
        done += opts.debian_install
        for line in work.debian_install():
            print line
    if opts.debian_changelog:
        done += opts.debian_changelog
        for line in work.debian_changelog():
            print line
    if opts.debian_rules:
        done += opts.debian_rules
        for line in work.debian_rules():
            print line
    if opts.debian_patches:
        done += opts.debian_patches
        for line in work.debian_patches():
            print line
    if opts.debian_scripts:
        done += opts.debian_scripts
        for line in work.debian_scripts():
            print line
    if opts.debian_dsc:
        done += opts.debian_dsc
        for line in work.debian_dsc():
            print line
    if opts.debian_diff:
        done += opts.debian_diff
        for line in work.debian_diff():
            print line
    auto = False
    if opts.d:
        if not opts.dsc:
            opts.dsc = spec+".dsc"
        if not opts.diff:
            if "3." in work.format:
                opts.diff = "%s_%s-0.debian.tar.gz" % (work.deb_source(), work.deb_version())
            else:
                opts.diff = "%s_%s-0.diff.gz" % (work.deb_source(), work.deb_version())
        if not opts.tar:
            opts.tar = "%s_%s.orig.tar.gz" % (work.deb_source(), work.deb_version())
        work.debtransform = False
        if not os.path.isdir(opts.d):
            os.mkdir(opts.d)
    elif not done and not opts.diff and not opts.dsc:
        auto = True
        if work.debtransform:
            work.debian_file = "debian.tar.gz"
        elif "3." in work.format:
            work.debian_file = spec+".debian.tar.gz"
        else:
            work.debian_file = spec+".debian.diff.gz"
        opts.dsc = spec+".dsc"
        opts.diff = work.debian_file
        _log.log(HINT, "automatically selecting -o %s -f %s", opts.dsc, opts.diff)
    if opts.tar:
        _log.log(DONE, work.write_debian_orig_tar(opts.tar, into = opts.d))
    if opts.diff:
        _log.log(DONE, work.write_debian_diff(opts.diff, into = opts.d))
    if opts.dsc:
        _log.log(DONE, work.write_debian_dsc(opts.dsc, into = opts.d))
    _log.info("converted %s packages from %s", len(work.packages), args)
    if opts.extract:
        cmd = "cd %s && dpkg-source -x %s" % (opts.d or ".", opts.dsc)
        _log.log(HINT, cmd)
        status, output = commands.getstatusoutput(cmd)
        if status:
            _log.fatal("dpkg-source -x failed with %s#%s:\n %s", status>>8, status&255, output)
        else:
            _log.info("%s", output)
    if opts.build:
        cmd = "cd %s && dpkg-source -b %s" % (opts.d or ".", work.deb_src())
        _log.log(HINT, cmd)
        status, output = commands.getstatusoutput(cmd)
        if status:
            _log.fatal("dpkg-source -b failed with %s#%s:\n %s", status>>8, status&255, output)
        else:
            _log.info("%s", output)
            
        

