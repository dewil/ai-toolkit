from .parser import parse

def load(path):
    return parse(open(path).read())
