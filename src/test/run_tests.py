#!/usr/lib/macq/dev-tools/virtualenv/bin/python3
""" run all tests """
import unittest
import sys

if __name__ == '__main__':
    loader = unittest.TestLoader()
    start_dir = '.'
    suite = loader.discover(start_dir)
    runner = unittest.TextTestRunner(buffer=True, verbosity=2)
    sys.exit(not runner.run(suite).wasSuccessful())
