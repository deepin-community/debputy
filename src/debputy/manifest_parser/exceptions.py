from debputy.exceptions import DebputyRuntimeError


class ManifestParseException(DebputyRuntimeError):
    pass


class ManifestTypeException(ManifestParseException):
    pass
