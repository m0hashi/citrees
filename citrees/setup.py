from setuptools import setup, find_packages

# python setup.py sdist upload

setup(
    name='citrees',
    version='0.0.0',
    url='https://github.com/rmill040/citrees',
    license='MIT',
    packages=find_packages(),
    install_requires=['pandas','numpy','scikit-learn',],
    python_requires='>=3.6',
    author='rmill040',
    author_email='',
    description='Bayesian conditional inference trees and forests in Python',
    keywords='machine-learning data structures trees citree conditional-inference-tree',
    classifiers=['License :: OSI Approved :: MIT License',]
)