import hashlib
import io
import re
import tarfile
import unittest
from pathlib import Path

from sync import SOURCE, update


class FormulaSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.current = Path(__file__).with_name("claudex.rb").read_text(encoding="utf-8")
        self.revision = "a" * 40
        previous = re.search(SOURCE, self.current)
        if previous is None:
            raise ValueError("The test formula has no source tag.")
        self.candidate = self.current.replace(f"v{previous[1]}", "v9999.0.0")
        self.candidate = re.sub(
            SOURCE,
            'url "https://github.com/nyorf/claudex.git", tag: "v9999.0.0", '
            f'revision: "{self.revision}"',
            self.candidate,
        )
        self.release = {
            "tag_name": "v9999.0.0",
            "draft": False,
            "prerelease": False,
            "assets": [],
        }
        for line in self.candidate.splitlines():
            if "sha256 cellar:" in line:
                tag = line.split(", ")[1].split(":")[0]
                digest = line.rsplit('"', 2)[1]
                self.release["assets"].append(
                    {
                        "name": f"claudex-9999.0.0.{tag}.bottle.tar.gz",
                        "state": "uploaded",
                        "digest": f"sha256:{digest}",
                    }
                )

    def archive(self, formula: str, *, symlink: bool = False) -> bytes:
        output = io.BytesIO()
        content = formula.encode()
        with tarfile.open(fileobj=output, mode="w:gz") as bundle:
            info = tarfile.TarInfo("Formula/claudex.rb")
            if symlink:
                info.type = tarfile.SYMTYPE
                info.linkname = "../../elsewhere"
                bundle.addfile(info)
            else:
                info.size = len(content)
                bundle.addfile(info, io.BytesIO(content))
        archive = output.getvalue()
        self.release["assets"] = [
            asset
            for asset in self.release["assets"]
            if asset["name"] != "claudex-homebrew-formula.tar.gz"
        ]
        self.release["assets"].append(
            {
                "name": "claudex-homebrew-formula.tar.gz",
                "state": "uploaded",
                "digest": "sha256:" + hashlib.sha256(archive).hexdigest(),
            }
        )
        return archive

    def test_matching_release_and_repeat_update(self) -> None:
        archive = self.archive(self.candidate)
        result = update(self.release, archive, self.revision, self.current, self.current)
        self.assertEqual(result, self.candidate)
        self.assertEqual(update(self.release, archive, self.revision, self.current, result), result)

    def test_archive_checksum_must_match(self) -> None:
        archive = self.archive(self.candidate)
        with self.assertRaisesRegex(ValueError, "archive checksum"):
            update(self.release, archive + b"changed", self.revision, self.current, self.current)

    def test_bottle_checksum_must_match(self) -> None:
        archive = self.archive(self.candidate)
        self.release["assets"][0]["digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "checksum for"):
            update(self.release, archive, self.revision, self.current, self.current)

    def test_cannot_replace_a_newer_formula(self) -> None:
        archive = self.archive(self.candidate)
        newer = self.candidate.replace("v9999.0.0", "v10000.0.0")
        with self.assertRaisesRegex(ValueError, "newer"):
            update(self.release, archive, self.revision, self.current, newer)

    def test_cannot_overwrite_a_changed_recipe(self) -> None:
        archive = self.archive(self.candidate)
        current = self.current.replace('license "Apache-2.0"', 'license "MIT"')
        with self.assertRaisesRegex(ValueError, "recipe has changed"):
            update(self.release, archive, self.revision, self.current, current)

    def test_archive_symlink_is_rejected(self) -> None:
        archive = self.archive(self.candidate, symlink=True)
        with self.assertRaisesRegex(ValueError, "archive is invalid"):
            update(self.release, archive, self.revision, self.current, self.current)

    def test_ruby_interpolation_in_cellar_is_rejected(self) -> None:
        candidate = re.sub(
            r'cellar: (?:\:[a-z_]+|"/[A-Za-z0-9._/-]+")',
            "cellar: \"/opt/#{system('false')}\"",
            self.candidate,
            count=1,
        )
        archive = self.archive(candidate)
        with self.assertRaisesRegex(ValueError, "bottle checksums"):
            update(self.release, archive, self.revision, self.current, self.current)

    def test_prerelease_is_rejected(self) -> None:
        archive = self.archive(self.candidate)
        self.release["prerelease"] = True
        with self.assertRaisesRegex(ValueError, "stable version"):
            update(self.release, archive, self.revision, self.current, self.current)


if __name__ == "__main__":
    unittest.main()
