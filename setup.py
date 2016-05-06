# -*- coding: utf-8 -*-

from setuptools import setup, find_packages


with open('README.rst') as f:
    readme = f.read()

with open('LICENSE') as f:
    license = f.read()

setup(
    name='PyHardware',
    version='0.0.1',
    description='My library for basic hardware communications',
    long_description=readme,
    author='Alan Borand',
    author_email='',
    url='https://github.com/borand/PyHardware.git',
    license=license,
    packages=find_packages(exclude=('tests', 'docs'))
)

