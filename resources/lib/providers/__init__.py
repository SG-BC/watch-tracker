from . import mdblist, simkl, wetrakr

ALL = (mdblist.PROVIDER, simkl.PROVIDER, wetrakr.PROVIDER)       # a new service is added here


def get(name):
    return next((p for p in ALL if p.name == name), None)


def enabled():
    return [p for p in ALL if p.enabled()]
