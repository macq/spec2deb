PKG = spec2deb
BUILDDIR = $(shell pwd)/build

prefix=/usr

default: build

build:
	cd src && python setup.py build --build-base=$(BUILDDIR)
	
install:
	: cd src && python setup.py --help install 
	cd src && sudo python setup.py build --build-base=$(BUILDDIR) \
	    install --prefix=$(prefix) --root=/
	
clean:
	cd src && sudo python setup.py clean --build-base=$(BUILDDIR)
	
help:          # shows this help
	@ cat $(MAKEFILE_LIST) | sed -e "/^[.a-z][-a-z0-9 _%]*:/!d" -e "s|: |:        |"

.PHONY: build install clean help
	