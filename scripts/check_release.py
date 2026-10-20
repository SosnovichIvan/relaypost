"""Only immutable MAJOR.MINOR.PATCH tags on remote main may be deployed."""

import os
import re
import subprocess


def check_release(ref_type: str, tag: str) -> None:
    if ref_type != "tag" or not re.fullmatch(
        r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", tag
    ):
        raise ValueError("Release requires a MAJOR.MINOR.PATCH tag")
    result = subprocess.run(  # noqa: S603 - fixed git invocation, no shell or user arguments
        ["/usr/bin/git", "merge-base", "--is-ancestor", "HEAD", "refs/remotes/origin/main"],
        check=False,
        capture_output=True,
    )
    if result.returncode:
        raise ValueError("Release commit must belong to the fetched remote main branch")


if __name__ == "__main__":
    check_release(os.environ["GITHUB_REF_TYPE"], os.environ["GITHUB_REF_NAME"])
