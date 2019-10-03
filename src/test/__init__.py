import os
import sys
import inspect
# The following three statements include the parent directory in the current path
# This makes it possible to directly include Python modules from the parent directory
CURRENT_DIR = os.path.dirname(os.path.abspath(
    inspect.getfile(inspect.currentframe())))
PARENT_DIR = os.path.dirname(CURRENT_DIR)

# As __init__.py is used twice during package import (meaning unittests would import the tests dir twice),
# it is better to check if the directory is not already in PYTHONPATH so we wouldn't overload it with duplicates.
# Also, even if it wasn't the case it is always better to check if a directory is not already in PYTHONPATH
# so you won't risk to have the interpreter panicking about finding two ways to the same module.
if not CURRENT_DIR in sys.path:
    sys.path.insert(0, CURRENT_DIR)

if not PARENT_DIR in sys.path:
    sys.path.insert(0, PARENT_DIR)
