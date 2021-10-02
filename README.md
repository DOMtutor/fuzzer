# Setup

## Requirements

* checktestdata: `build-essentials`, `automake`, `libboost-dev`, `libgmp-dev`
* compilation: `pypy3` (and Java, C++, ...)

## Procedure

* `git submodule update --init --recursive`
* `cd problemtools/support && make`
* `python3 -m venv venv`
* `. ./venv/bin/activate`
* `pip install -r requirements`
* Add link to problem repository `ln -s <path> repository`
