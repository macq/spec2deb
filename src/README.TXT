## AUTHORS ##

   Guido Draheim <guidod@gmx.de>

## INTRODUCTION ##

   The script assumes a fairly simple rpm spec file as input. If the upstream
   project follows the normal GNU-style of "configure && make && make install"
   then the %script sections don't need to name much more than that. In the
   real world the packager will add and remove some files as well as shifting
   files from one place to another. But that's okay for the spec2deb script as
   well.
   
   Anything else will probably fail. Here's a list of things it CAN do:
   
   * %if/%endif around dependencies per package: any if-condition that names
      something called "debian" will make the section to be kept while all the 
      other conditions will make the scanner to ignore the lines between the
      %if and %endif
   * setting defaults in the single-line style of
     %{!?variable: %define variable value}
     %{!?variable: %global variable value}
     %{!?variable: %{?other: %define variable value}}
     %{!?variable: %{?other: %global variable value}}
   * %setup and %setup -n other-dir
     will make the script to know that there will be subdirectory in the
     tarball either named "%{name}-%{version}" or whatever value was given
     after the "-n" option to setup. That's important for the debian.diff.gz
     as dpkg-source will apply it BEFORE the debian build will chdir to it.
   * Obviously, any rpm-%{variable} will get fully expanded in most cases,
     with the exception of the predefined %_macros that will be put as
     makefile macros in the final debian/rules file. Note that underscore
     at the beginning of predefined macros that will make for the difference.
   * %buildroot is substituted by "$(CURDIR)/debian/tmp" in debian/rules 
     (no matter what your buildroot:-setting in the rpm spec had been)
     and %_make will always be $(MAKE). Those are exceptions however as all
     other values get the value that they are being set to in the rpm spec 
     or the rpm macros default (although only a part of them have been added).
     
  What it can ALMOST do:
  
   * Multiple Source:-files. Or more specifically ... more than one source 
     tarball that needs to be unpacked in the build sources area. Only the
     first Source:-file will be unpacked - all the other source-files will
     be converted into patches. So they should better be plain text files.
   * The Patch:-files are pushed to the debian/series of course that will
     also specify the order of applying patches. The current order is taken
     from the number if the declaration, i.e. it is not the order of
     appearance in in the %setup section (that would be a TODO then).
   * The postinst/prerm scripts are converted and there is a header section
     that maps "install/upgrade" to the "$1" 0/1/2 style of rpm scripts. But
     the debian style has more steps and modes which are NOT covered correctly. 
     So you should keep your "maintainer scripts" rather simple so that it
     can get away with being called multiple times during install/remove. 
     
## FORMATS ##

   The script does essentially support three formats that it can generate.
   
   * FORMAT 1.0 - this will generate a package-version.diff.gz containing
     the debian files. The dpkg-source will expand it with diff creating 
     the extra package/debian/files in the source area of the build.
   * FORMAT 3.0 - this will generate a package-version.debian.tar.gz instead
     of a diff.gz. The dpkg-source will unpack it thereby creating again the 
     extra package/debian/files in the source area of the build.
   * DEBFORMAT - for OBS (Opensuse Buildsystem) which has the peculiarity
     that it will take a debian.tar.gz as input (NO package prefix!) in order
     to generate a FORMAT 1.0 diff.gz on the build server.
      
   You can also explicitly specify a "-f debian.tar.gz" for a FORMAT 1.0 
   target but it is simply not the default. The spec2deb script handles all
   the extra debian/files internally as patches that will only be converted
   to tar entries if you want it to be done that way. The default is the
   FOMRAT 1.0 of course - you can change that with:
   
   * spec2deb.py --format=3 --no-debtransform mypackage.spec
   
## TESTING ##

   In general all of the additional debian source files are generated right
   next to the original *.spec and tarball. So if you have a ./mypackage.spec
   then a ./mypackage.dsc and ./mypackage-version.diff.gz will appear after 
   the spec2deb run.  
   
   Note however that in this case the original tarball is not converted. This
   fine for the DEBFORMAT case which will do the conversion when copying the 
   input to the build server anyway. It is also okay if you do have a tarball 
   in the directory that is named "package_version.orig.tar.gz" because that 
   name format is strictly required by dpkg-source. However it is very 
   questionable that you have downloaded it with that name from the upstream 
   project. 
   
   If you do specify the "-d sources" option to make the generation of the
   debian files to occur in another (sub)directory then the default will be
   to convert the original tarball as well thereby creating a copy (or a
   recompressed version of it in the *.bz2 case) in the target area that is
   named "package_version.orig.tar.gz". Just as dpkg-source likes it.
    
   So that -d sources directory will then be ready for a test with dpkg-source.
   And for a matter of fact, if you do specify "-x" then "dpkg-source -x" is 
   called and the same applies to "-b" running "dpkg-source -b". That is a
   good smoke test if the generated debian files are good as it will unpack
   the sources into a sources area ready for build (although no real build is
   done). So you might want to try
   
   * spec2deb.py mypackage.spec -d sources -x -b
   
## OBS TESTING ##

   Note that if "spec2deb.py" is called without any options at all ... and 
   there is an ".osc" subdirectory then the DEBFORMAT is chosen. Also all 
   debian files are generated to the local directory which is then ready 
   for "osc commit". That commit will trigger a Debian build of the associated 
   OBS project at build.opensuse.org. 
   
   So the command line options are basically optimized for that usage: after 
   having a change on the rpm parts of an OBS project you can easily regenerate
   the associated DEB build info (package.dsc, debian.tar.gz) by just typing
   
   * spec2deb.py
       
   The script will check the current directory for an *.spec file in that case
   and if exactly one is found then it is used the first argument that would
   otherwise be required. Of course ... in some sophisticated OBS RPM project
   there may be multiple *.spec files optimizing for different build targets.

   But again: spec2deb shall help you with the normal and not that overly 
   complex case. Try to keep the differences of each build target in one
   *.spec file with %if-%endif being your friend to care for the differing
   names of the build-dependency package names (as each distro has another
   naming policy to follow). Using %if.*debian you can even add some extra
   spec hints for the debian build targets (although the spec is never being
   executed for those - just the spec2deb generated *.dsc and debian/rules).
     
