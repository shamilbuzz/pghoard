#!/bin/sh
# Activate project's virtual environment
python setup.py bdist_egg
easy_install dist/pghoard-2.1.0.dev1-py3.8.egg