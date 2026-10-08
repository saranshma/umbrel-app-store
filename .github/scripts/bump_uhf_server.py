import json
import os
import re
import sys
import urllib.request

APP_DIR = "saransh-uhf-server"
MANIFEST = f"{APP_DIR}/umbrel-app.yml"
COMPOSE = f"{APP_DIR}/docker-compose.yml"


def get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def set_output(name, value):
    # Works locally too: GITHUB_OUTPUT is unset outside Actions, so this is a no-op there.
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a") as f:
        f.write(f"{name}={value}\n")


def main():
    with open(MANIFEST) as f:
        manifest = f.read()
    current_version = re.search(r'^version: *"?([^"\n]+)"?', manifest, re.MULTILINE).group(1)
    print(f"Currently packaged version: {current_version}")

    tags_data = get_json("https://hub.docker.com/v2/repositories/swapplications/uhf-server/tags?page_size=25")
    tags = [t["name"] for t in tags_data["results"] if re.fullmatch(r"\d+\.\d+\.\d+", t["name"])]
    tags.sort(key=lambda v: tuple(int(p) for p in v.split(".")), reverse=True)
    if not tags:
        print("Could not determine latest version, aborting.")
        return 0
    latest_version = tags[0]
    print(f"Latest upstream version: {latest_version}")

    if latest_version == current_version:
        print("Already up to date.")
        return 0

    token = get_json(
        "https://auth.docker.io/token?service=registry.docker.io&scope=repository:swapplications/uhf-server:pull"
    )["token"]
    manifest_list = get_json(
        f"https://registry-1.docker.io/v2/swapplications/uhf-server/manifests/{latest_version}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.docker.distribution.manifest.list.v2+json,"
            "application/vnd.oci.image.index.v1+json",
        },
    )
    amd64 = [m for m in manifest_list.get("manifests", []) if m.get("platform", {}).get("architecture") == "amd64"]
    if not amd64:
        print(f"Could not resolve an amd64 digest for {latest_version}, aborting.")
        return 0
    digest = amd64[0]["digest"]
    print(f"amd64 digest: {digest}")

    with open(COMPOSE) as f:
        compose = f.read()
    new_compose, n = re.subn(
        r"image: swapplications/uhf-server:[0-9.]+@sha256:[a-f0-9]+",
        f"image: swapplications/uhf-server:{latest_version}@{digest}",
        compose,
    )
    if n != 1:
        print(f"Expected exactly one image line to replace, found {n}. Aborting without writing.")
        return 1
    with open(COMPOSE, "w") as f:
        f.write(new_compose)

    new_manifest, n = re.subn(
        r'^version: *"?[^"\n]+"?',
        f'version: "{latest_version}"',
        manifest,
        count=1,
        flags=re.MULTILINE,
    )
    if n != 1:
        print(f"Expected exactly one version line to replace, found {n}. Aborting without writing.")
        return 1

    marker = "releaseNotes: >-\n"
    idx = new_manifest.index(marker) + len(marker)
    note = f"  {latest_version}: automatic update to the latest upstream release.\n"
    new_manifest = new_manifest[:idx] + note + new_manifest[idx:]

    with open(MANIFEST, "w") as f:
        f.write(new_manifest)

    print(f"Bumped {APP_DIR} to {latest_version}")
    set_output("version", latest_version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
