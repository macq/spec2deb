#!/usr/lib/macq/dev-tools/virtualenv/bin/python3
# vim: fileencoding=utf-8 ts=4 et sw=4 sts=4
""" Unit tests """
from contextlib import redirect_stdout
import gzip
import io
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
from unittest.mock import patch, call

from spec2deb import spec2deb


class TestMacqSpec2Deb(unittest.TestCase):
    """ Test the spec2deb.py command line interface and output """

    @classmethod
    def setUpClass(cls):
        cls.maxDiff = None
        cls.tmp_dir = tempfile.mkdtemp()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp_dir)

    def test_pkg_with_list_of_files(self):
        Path(self.tmp_dir + '/pkg-1.2.3.tgz').touch()

        captured_output = io.StringIO()
        with redirect_stdout(captured_output):
            spec2deb.main(
                ["test_data/pkg.spec", "-d", self.tmp_dir, "-p", self.tmp_dir])
        output = captured_output.getvalue()
        self.assertEqual("----------------- sourcefile {}/pkg-1.2.3.tgz\n"
                         "".format(self.tmp_dir), output)

        final_diff_gz = self.tmp_dir + "/pkg_1.2.3-4.diff.gz"
        self.assertTrue(os.path.exists(final_diff_gz))
        with open('test_data/pkg_1.2.3-4.diff') as file_expected:
            with gzip.open(final_diff_gz, 'rb') as f:
                expected_output = file_expected.read()
                real_output = f.read().decode()
            self.assertEqual(expected_output, real_output)


if __name__ == '__main__':
    unittest.main()
