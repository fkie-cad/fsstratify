# fsstratify


## Installation
The following sections give a brief overview of how to install fsstratify and its dependencies.

fsstratify requires at least Python 3.9.

If you want to install fsstratify from this git repository simply run:

```console
$ pip install .
```

If you want to run the tests or if you want to contribute code to fsstratify, you can add the `tests` and `dev` extras to this command:

```console
$ pip install ".[tests]"  # this installs only the dependencies to run the tests
$ pip install ".[dev]"    # this installs additonal dev dependencies and everything from tests
$ pip install ".[all]"    # this installs all dependecies for all possible use cases
```

## Documentation
The `docs` target build the documentation and the `serve-docs` target starts a local web server
so that you can browse the documentation.

```console
$ make docs
$ make serve-docs
```

## Citation

If you use fsstratify in your research, please cite our paper:

**Full Citation:**
> Julian Uthoff; Lisa MarieDreier; Martin Lambertz; Mariia Rybalka; Felix Freiling (2025). Creating a standardized corpus for digital stratigraphic methods with fsstratify. *Forensic Science International: Digital Investigation*, 54, 301986. https://doi.org/10.1016/j.fsidi.2025.301986

**BibTeX:**
```bibtex
@article{UTHOFF2025301986,
  title = {Creating a standardized corpus for digital stratigraphic methods with fsstratify},
  author = {Julian Uthoff and Lisa Marie Dreier and Martin Lambertz and Mariia Rybalka and Felix Freiling},
  journal = {Forensic Science International: Digital Investigation},
  volume = {54},
  pages = {301986},
  year = {2025},
  note = {DFRWS APAC 2025 - Selected Papers from the 5th Annual Digital Forensics Research Conference APAC},
  issn = {2666-2817},
  doi = {10.1016/j.fsidi.2025.301986}
}
```

