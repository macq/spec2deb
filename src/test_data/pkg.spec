Name:          pkg
Version:       1.2.3
Release:       4
License:       Proprietary
Summary:       project summary
Group:         Applications
Vendor:        some-vendor
Url:           http://some-url/
BuildRequires: package1-devel = 1.2.3-0
BuildRequires: package2-devel == 2.0.0-0
BuildRequires: package3-devel > 1.2.3-0
BuildRequires: package4-devel >= 1.2.3-0
BuildRequires: package5-devel => 1.2.3-0
BuildRequires: package6-devel < 1.2.3-0
BuildRequires: package7-devel <= 1.2.3-0
BuildRequires: package8-devel =< 1.2.3-0
BuildRequires: package9-devel
Requires:      package1 = 1.2.3-0
Requires:      package2 == 2.0.0-0
Requires:      package3 > 1.2.3-0
Requires:      package4 >= 1.2.3-0
Requires:      package5 => 1.2.3-0
Requires:      package6 < 1.2.3-0
Requires:      package7 <= 1.2.3-0
Requires:      package8 =< 1.2.3-0
Requires:      package9
Provides:      prov-pkg1 = 1.2.3-0
Provides:      prov-pkg2 == 2.0.0-0
Provides:      prov-pkg3 > 1.2.3-0
Provides:      prov-pkg4 >= 1.2.3-0
Provides:      prov-pkg5 => 1.2.3-0
Provides:      prov-pkg6 < 1.2.3-0
Provides:      prov-pkg7 <= 1.2.3-0
Provides:      prov-pkg8 =< 1.2.3-0
#Provides:      prov-pkg9 #this is now converted like prov-pkg9 (= 0.0.0); not sure that is right.
Obsoletes:     previous-pkg-name
Conflicts:     conflicting-pkg

%if 0%{?some_variable_that_does_not_exist}
Requires:      should-not-get-here
%define _git 123
%else
Requires:       git >= 1.8.3
%define _git 0
%endif

BuildRoot:     %{_tmppath}/%{name}-%{version}-build
Source:        %{name}-%{version}.tgz

%description
%{summary}

%package devel
Group:         Development/Libraries
Summary:       Development files for %{name}
Requires:      %{name} = %{version}-%{release}

%description devel
%{summary}

%package -n other-pkg
Group:         Applications
Summary:       other pkg

%description -n other-pkg

%package -n other-pkg-devel
Group:         Development/Libraries
Summary:       Development files for other-pkg
Requires:      other-pkg = %{version}-%{release}

%description -n other-pkg-devel
%{summary}

%package x
Group:         Development/Libraries
Summary:       other dependencies
Requires:      other-pkg = %{version}-%{release}
Requires:      some-x-package

%description x
%{summary}

%package with-dash
Group:         Applications
Summary:       with-dash subpackage
Requires:      %{name} = %{version}-%{release}

%description with-dash
%{summary}

%prep
%setup -q

%build

%install
touch ${RPM_BUILD_ROOT}/file1
install -d ${RPM_BUILD_ROOT}/dir2
touch ${RPM_BUILD_ROOT}/dir2/a
touch ${RPM_BUILD_ROOT}/dir2/b
touch ${RPM_BUILD_ROOT}/dir2/c
touch gitconfig{0,1,2}
install -d ${RPM_BUILD_ROOT}/etc
install -m 0644 gitconfig%_git ${RPM_BUILD_ROOT}/etc/gitconfig
find $RPM_BUILD_ROOT -type d | sed 's|'$RPM_BUILD_ROOT'|%dir |' >  list-of-files
find $RPM_BUILD_ROOT -type f -o -type l | sed 's|'$RPM_BUILD_ROOT'||' >> list-of-files

%pre
%if "%{?_vendor}" == "bogus"
echo "redhat vendor"
%else
echo "other vendor"
%endif

%files -f list-of-files
%defattr(1754,user1,group1)
/dir2/

%files devel
/dir2/a

%files -n other-pkg
/dir2/b

%files -n other-pkg-devel
/dir2/c

%files x

%files with-dash
/dir2/c

%changelog
