from .schema import validate

def parse(text):
    return validate(dict(l.split('=') for l in text.splitlines() if l))
