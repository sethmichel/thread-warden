1) fully decouple the doctor thread from my trading suit
2) nothing in there can be specific to trading, it needs to be generic and abstract
3) using typeing a lot since that's modern
    python is dyncamially typed, meaning I don't declare a variable is a string or int. type hinting (python 3.5+) lets me set this. this gives the user the correct autocomplete. it also helps with bug catching
    ex) def register_thread(thread: Thread, timeout: int = 30) -> None:
4) use the loggin library instead of print so users can connect to it
5) need a new name, like py-thread-supervisor


thread-warden/
├── .github/
│   └── workflows/       # CI/CD (Vital for SDE 2 resumes)
│       └── tests.yml
├── src/
│   └── thread_warden/   # The actual package source
│       ├── __init__.py
│       ├── supervisor.py
│       └── interfaces.py
├── tests/               # Pytest files
├── examples/            # Runnable scripts showing usage
├── pyproject.toml       # Modern configuration (replaces setup.py)
├── README.md            # The most important file
├── LICENSE              # MIT or Apache 2.0
└── .gitignore


open source aspect
1) need pyproject.toml and not setup.py. use build tools like hatch or poetry or the basic setuptools with a toml config
2) ci/cd github actions. needs to run pytest whenever I push the code, that's teh little green badge on readme


so the structure is the yml file talks to github and triggers the ci/cd thing that gets the build passing badge on my readme and it does this by running the pytest file. And then the build is done by pyproject.toml which uses basically a build library like hatch, poetry, setuptools.
    - when I git push, github will find my tests.yml file, that tells it to install python and run these commands.
    - toml is the blueprint of metadata. the build tool is the construction crew that builds the code into an installable file

pypl and pip installs
- pypl is easy. it's public forever though so nothing personal and no secrets
- you can pip install directlry using the github link
    - or you run twine upload dist/*, then the user runs pip install x. pip asks pypl for my package