import json
import pathlib

ACTIONS_CHECKOUT = {"name": "Check out repository", "uses": "actions/checkout@v5"}
THIS_FILE = pathlib.PurePosixPath(
    pathlib.Path(__file__).relative_to(pathlib.Path().resolve())
)


def gen(content: dict, target: str):
    pathlib.Path(target).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(target).write_text(
        json.dumps(content, indent=2, sort_keys=True), newline="\n"
    )


def gen_publish_workflow():
    target = ".github/workflows/publish.yaml"
    content = {
        "env": {
            "description": f"This workflow ({target}) was generated from {THIS_FILE}",
        },
        "name": "Publish the package to PyPI",
        "on": {"release": {"types": ["published"]}},
        "jobs": {
            "publish": {
                "name": "Publish the package to PyPI",
                "runs-on": "ubuntu-latest",
                "environment": {
                    "name": "pypi-release",
                    "url": "https://pypi.org/p/bibliocommons",
                },
                "permissions": {"id-token": "write"},
                "steps": [
                    ACTIONS_CHECKOUT,
                    {"name": "Publish the package to PyPI", "run": "sh ci/publish.sh"},
                ],
            }
        },
    }
    gen(content, target)


def main():
    gen_publish_workflow()


if __name__ == "__main__":
    main()
