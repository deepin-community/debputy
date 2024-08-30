from debputy.exceptions import DebputyRuntimeError


class ManifestException(DebputyRuntimeError):
    pass


class ManifestParseException(ManifestException):
    pass


class ManifestTypeException(ManifestParseException):
    pass


class ManifestInvalidUserDataException(ManifestException):
    pass
