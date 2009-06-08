#!/bin/sh
# Build and install.
./build.py -lt
sudo python setup.py install --install-layout=deb --force
