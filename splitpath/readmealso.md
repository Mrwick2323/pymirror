i don't really want to write a ton for this;

splitpath is just pymirror.py split into two files, one for getting immediate site data, and one for collecting and hosting data from other paths the webapp calls

usages:
```
python3 site_mirror.py https://domain.you/want/to/clone -o ./folder/to/clone/to
python3 mirror_server.py -d https://domain.you/want/to/clone -f ./folder/to/clone/to/that/contains/index.html
# you MUST pick the one with index.html on mirror_server; it will NEVER be the same path as the last one, but it will be a subdirectory of it
```
