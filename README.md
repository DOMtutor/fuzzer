# Setup

## Requirements
 * checktestdata: `automake`, `libboost-dev`, `libgmp-dev`
 * compilation: `pypy3`

## Procedure

 * `git submodule update --init --recursive`
 * `cd problemtools/support && make`
 * `python3 -m venv venv`
 * `. ./venv/bin/activate`
 * `pip install -r requirements`
