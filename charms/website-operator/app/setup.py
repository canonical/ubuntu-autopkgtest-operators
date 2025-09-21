"""
Setup file for autopkgtest-web python package
"""

from setuptools import find_packages, setup

setup(
    name="webcontrol",
    version="0.0",
    description="autopkgtest web control",
    author="Ubuntu Foundations",
    packages=find_packages(),
    include_package_data=True,
)
