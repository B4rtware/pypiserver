import hashlib
import itertools
import os
import typing as t
from pathlib import Path

from . import Configuration
from .cache import CacheManager
from .pkg_helpers import (
    normalize_pkgname,
    parse_version,
    is_allowed_path,
    guess_pkgname_and_version,
)

PathLike = t.Union[str, os.PathLike]


class PkgFile:
    __slots__ = [
        "pkgname",  # The projects/package name with possible capitalization
        "version",  # The package version as a string
        "fn",  # The full file path
        "root",  # An optional root directory of the file
        "relfn",  # The file path relative to the root
        "replaces",  # The previous version of the package (used by manage.py)
        "pkgname_norm",  # The PEP503 normalized project name
        "digest",  # The file digest in the form of <algo>=<hash>
        "relfn_unix",  # The relative file path in unix notation
        "parsed_version",  # The package version as a tuple of parts
        "digester",  # a function that calculates the digest for the package
    ]
    digest: t.Optional[str]
    digester: t.Optional[t.Callable[["PkgFile"], t.Optional[str]]]
    parsed_version: tuple
    relfn_unix: t.Optional[str]

    def __init__(
        self,
        pkgname: str,
        version: str,
        fn: t.Optional[str] = None,
        root: t.Optional[str] = None,
        relfn: t.Optional[str] = None,
        replaces: t.Optional["PkgFile"] = None,
    ):
        self.pkgname = pkgname
        self.pkgname_norm = normalize_pkgname(pkgname)
        self.version = version
        self.parsed_version: tuple = parse_version(version)
        self.fn = fn
        self.root = root
        self.relfn = relfn
        self.relfn_unix = None if relfn is None else relfn.replace("\\", "/")
        self.replaces = replaces
        self.digest = None

    def __repr__(self) -> str:
        return "{}({})".format(
            self.__class__.__name__,
            ", ".join(
                [
                    f"{k}={getattr(self, k, 'AttributeError')!r}"
                    for k in sorted(self.__slots__)
                ]
            ),
        )

    @property
    def fname_and_hash(self) -> str:
        if self.digest is None and self.digester is not None:
            self.digest = self.digester(self)
        hashpart = f"#{self.digest}" if self.digest else ""
        return self.relfn_unix + hashpart  # type: ignore


class Backend:
    def __init__(self, config: Configuration):
        self.hash_algo = config.hash_algo  # type: ignore

    def get_all_packages(self) -> t.Iterable[PkgFile]:
        """Implement this method to return an Iterable of all packages (as
        PkgFile objects) that are available in the Backend.
        """
        raise NotImplementedError

    def add_package(self, filename: str, stream: t.BinaryIO) -> None:
        """Add a package to the Backend. `filename` is the package's filename
        (without any directory parts). It is just a name, there is no file by
        that name (yet). `fh` is an open file object that can be used to read
        the file's content. To convert the package into an actual file on disk,
        run `as_file(filename, fh)`.
        """
        raise NotImplementedError

    def remove_package(self, pkg: PkgFile) -> None:
        """Remove a package from the Backend"""
        raise NotImplementedError

    def exists(self, filename: str) -> bool:
        """Does a package by the given name exist?"""
        raise NotImplementedError

    def digest(self, pkg: PkgFile) -> t.Optional[str]:
        if self.hash_algo is None or pkg.fn is None:
            return None
        return digest_file(pkg.fn, self.hash_algo)

    def package_count(self) -> int:
        """Return a count of all available packages. When implementing a Backend
        class, either use this method as is, or override it with a more
        performant version.
        """
        return sum(1 for _ in self.get_all_packages())

    def get_projects(self) -> t.Iterable[str]:
        """Return an iterable of all (unique) projects available in the store
        in their PEP503 normalized form. When implementing a Backend class,
        either use this method as is, or override it with a more performant
        version.
        """
        return set(package.pkgname_norm for package in self.get_all_packages())

    def find_project_packages(self, project: str) -> t.Iterable[PkgFile]:
        """Find all packages from a given project. The project may be given
        as either the normalized or canonical name. When implementing a
        Backend class, either use this method as is, or override it with a
        more performant version.
        """
        return (
            x
            for x in self.get_all_packages()
            if normalize_pkgname(project) == x.pkgname_norm
        )

    def find_version(self, name: str, version: str) -> t.Iterable[PkgFile]:
        """Return all packages that match PkgFile.pkgname == name and
        PkgFile.version == version` When implementing a Backend class,
        either use this method as is, or override it with a more performant
        version.
        """
        return filter(
            lambda pkg: pkg.pkgname == name and pkg.version == version,
            self.get_all_packages(),
        )


class SimpleFileBackend(Backend):
    def __init__(self, config: Configuration, roots: t.List[PathLike]):
        super().__init__(config)
        self.roots = [Path(root).resolve() for root in roots]

    def get_all_packages(self) -> t.Iterable[PkgFile]:
        return itertools.chain.from_iterable(listdir(r) for r in self.roots)

    def add_package(self, filename: str, stream: t.BinaryIO) -> None:
        write_file(stream, self.roots[0].joinpath(filename))

    def remove_package(self, pkg: PkgFile) -> None:
        if pkg.fn is not None:
            os.remove(pkg.fn)

    def exists(self, filename: str) -> bool:
        # TODO: Also look in subdirectories?
        return any(root.joinpath(filename).exists() for root in self.roots)


class CachingFileBackend(SimpleFileBackend):
    def __init__(
        self,
        config: Configuration,
        roots: t.List[PathLike],
        cache_manager: CacheManager,
    ):
        super().__init__(config, roots)

        self.cache_manager = cache_manager

    def get_all_packages(self) -> t.Iterable[PkgFile]:
        return itertools.chain.from_iterable(
            self.cache_manager.listdir(r, listdir) for r in self.roots
        )

    def digest(self, pkg: PkgFile) -> t.Optional[str]:
        if pkg.fn is None:
            return None
        return self.cache_manager.digest_file(
            pkg.fn, self.hash_algo, digest_file
        )


def write_file(fh: t.BinaryIO, destination: PathLike) -> None:
    """write a byte stream into a destination file. Writes are chunked to reduce
    the memory footprint
    """
    chunk_size = 2 ** 20  # 1 MB
    offset = fh.tell()
    try:
        with open(destination, "wb") as dest:
            for chunk in iter(lambda: fh.read(chunk_size), b""):
                dest.write(chunk)
    finally:
        fh.seek(offset)


def listdir(root: PathLike) -> t.Iterable[PkgFile]:
    root = Path(root).resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [x for x in dirnames if is_allowed_path(x)]
        for x in filenames:
            fn = os.path.join(root, dirpath, x)
            if not is_allowed_path(x) or not Path(fn).is_file():
                continue
            res = guess_pkgname_and_version(x)
            if not res:
                # Seems the current file isn't a proper package
                continue
            pkgname, version = res
            if pkgname:
                yield PkgFile(
                    pkgname=pkgname,
                    version=version,
                    fn=fn,
                    root=str(root),
                    relfn=fn[len(str(root)) + 1 :],
                )


def digest_file(file_path: PathLike, hash_algo: str) -> str:
    """
    Reads and digests a file according to specified hashing-algorith.

    :param file_path: path to a file on disk
    :param hash_algo: any algo contained in :mod:`hashlib`
    :return: <hash_algo>=<hex_digest>

    From http://stackoverflow.com/a/21565932/548792
    """
    blocksize = 2 ** 16
    digester = hashlib.new(hash_algo)
    with open(file_path, "rb") as f:
        for block in iter(lambda: f.read(blocksize), b""):
            digester.update(block)
    return f"{hash_algo}={digester.hexdigest()}"
