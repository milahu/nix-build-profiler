from setuptools import setup

with open('requirements.txt') as f:
    install_requires = f.read().splitlines()

setup(
  name='nix-build-profiler',
  version='0.0.1',
  #author='...',
  #description='...',
  install_requires=install_requires,
  entry_points={
    # example: file some_module.py -> function main
    'console_scripts': ['nix-build-profiler=nix_build_profiler:main']
  },
)
