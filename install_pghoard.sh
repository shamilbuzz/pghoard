#!/bin/sh
# Activate project's virtual environment
git checkout 2.1.0-update
git pull
python uninstall pghoard
python setup.py sdist
pip3 install dist/pghoard-2.1.0.dev101.tar.gz
