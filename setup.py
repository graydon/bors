from setuptools import setup

from bors import __version__

setup(
    name='bors',
    version=__version__,
    description='A continuous integration and automatic landing system for github pull requests.',
    author='Graydon Hoare',
    py_modules=['bors', 'github'],
    entry_points={
        'console_scripts': ['bors = bors:main'],
        'github': ['github = github']
    },
    zip_safe=False,
)
