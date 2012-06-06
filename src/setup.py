#!/usr/bin/env python
# -*- coding: utf8 -*-

from distutils.core import setup

README = open("README.TXT").read()
 
setup(
  package_data = { "spec2deb" : [ "spec2deb.py" ]},
  name = "spec2deb",
  version = "0.2",
  packages = [ "spec2deb" ],
  keywords = 'rpm spec deb debian converter',
  author = 'Guido Draheim',
  author_email = 'guidod@gmx.de',
  url = 'http://bitbucket.org/guidod/spec2deb',
  description = README,
  license = '''BSD''',
)
