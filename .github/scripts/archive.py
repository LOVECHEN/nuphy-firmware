"""Download official firmware on GitHub runners and publish renamed Release assets."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import unicodedata
from urllib.parse import quote, unquote, urlencode, urlsplit
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

REPOSITORY = "LOVECHEN/nuphy-firmware"
BLOCKED = "1056de181839627856178383d3df2c192c7b1793de443e47ad0c9ec1853c99b7"


def digest(data):
    return sha256(data).hexdigest()


def mask(value):
    if os.environ.get("GITHUB_ACTIONS") == "true":
        value = str(value).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print("::add-mask::" + value, flush=True)


def config():
    value = json.loads(os.environ["NUPHY_UPSTREAM_CONFIG"])
    if set(value) != {"api", "allowedHosts"} or set(value["api"]) != {"keyboards", "dongles", "radios", "latest", "history"}:
        raise ValueError("Invalid source configuration")
    for item in [*value["api"].values(), *value["allowedHosts"]]:
        mask(item)
    return value


def valid_url(value, settings):
    url = urlsplit(value)
    if any(char in value for char in "\r\n") or url.scheme != "https" or url.hostname not in settings["allowedHosts"] or url.username or url.password or url.port not in (None, 443):
        raise ValueError("Unexpected upstream URL")
    return value


def get(url, settings, limit):
    valid_url(url, settings)
    mask(url)
    for attempt in range(4):
        try:
            with urlopen(Request(url, headers={"User-Agent": "NuPhy-official-firmware-archive/1"}), timeout=60) as response:
                valid_url(response.url, settings)
                mask(response.url)
                data = response.read(limit + 1)
            if len(data) > limit:
                raise ValueError("Response size limit exceeded")
            return data
        except OSError:
            if attempt == 3:
                raise
            time.sleep(attempt + 1)


def slug(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    value = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")
    if not value or len(value) > 100:
        raise ValueError("Invalid filename")
    return value


def collect(api):
    keyboards, dongles, radios = [api(name) for name in ("keyboards", "dongles", "radios")]
    if not all(isinstance(rows, list) for rows in (keyboards, dongles, radios)):
        raise ValueError("Invalid catalog")
    targets = []
    for rows, key, component in ((keyboards, "keyBoardId", "Main"), (dongles, "dongleId", "Dongle"), (radios, "rfId", "RF")):
        for item in rows:
            identity = str(item[key])
            if not re.fullmatch(r"[0-9]{1,64}", identity):
                raise ValueError("Invalid business ID")
            parents = [row for row in keyboards if identity in str(row.get("rfIds") or "").split(",")] if component == "RF" else []
            if component == "RF" and len(parents) != 1:
                raise ValueError("RF parent is not unique")
            parent = parents[0] if parents else item
            targets.append({"businessID": identity, "modelID": str(parent["keyBoardId"] if parents else identity),
                            "model": parent["name"].strip(), "component": component,
                            "category": "receiver" if component == "Dongle" else {"1": "he", "2": "he", "3": "mechanical"}.get(str(parent.get("type")), "other")})
    if not targets or len(targets) > 1024 or len({row["businessID"] for row in targets}) != len(targets):
        raise ValueError("Invalid targets")

    def versions(target):
        identity = target["businessID"]
        responses = [api("latest", {"businessId": identity, "type": channel}) for channel in (1, 2)]
        responses.append(api("history", {"businessId": identity, "excludeLastVersion": "Y"}))
        entries = {}
        for response in responses:
            for item in response if isinstance(response, list) else [response] if response else []:
                if str(item["businessId"]) != identity:
                    raise ValueError("Firmware business ID mismatch")
                identifier = str(item["firmwareVersionsId"])
                if not identifier.isdecimal():
                    raise ValueError("Invalid release ID")
                row = {**target, "id": identity + ":" + identifier, "version": str(item["versionNumber"]),
                       "channel": "Beta" if str(item.get("type")) == "2" else "Stable", "releasedAt": item.get("firstListTime") or "",
                       "officialURL": item.get("firmwareFileUrl"), "imageSHA256": item.get("sha256")}
                if row["imageSHA256"] and not re.fullmatch(r"[a-f0-9]{64}", row["imageSHA256"]):
                    raise ValueError("Invalid image hash")
                if row["id"] in entries and entries[row["id"]] != row:
                    raise ValueError("Conflicting version metadata")
                entries[row["id"]] = row
        return list(entries.values())

    entries = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        for batch in pool.map(versions, targets):
            entries.extend(batch)
    if not entries or len(entries) > 10000:
        raise ValueError("Invalid firmware count")
    return targets, sorted(entries, key=lambda row: row["id"])


def inspect_package(data, expected, extension):
    if extension == ".bin":
        if len(data) > 8 * 1024 * 1024:
            raise ValueError("Image size limit exceeded")
        images = [("application.bin", data)]
    elif extension == ".zip":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > 128 or any(item.file_size > 8 * 1024 * 1024 for item in members) or sum(item.file_size for item in members) > 32 * 1024 * 1024:
                raise ValueError("ZIP size limit exceeded")
            images = [(item.filename, archive.read(item)) for item in members if item.filename.lower().endswith(".bin")]
    else:
        raise ValueError("Unexpected package format")
    matches = [(name, image) for name, image in images if digest(image) == expected]
    if not matches:
        raise ValueError("No BIN matches official SHA256")
    return matches[0]


def stem(row):
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", row["version"]):
        raise ValueError("Invalid version filename")
    name = slug(row["model"])
    component = "" if row["component"] == "Dongle" and name.lower().endswith("dongle") else "_" + row["component"]
    return name + component + "_v" + row["version"] + "_" + row["channel"] + "_" + row["imageSHA256"][:8]


def cell(value):
    return str(value).replace("|", "\\|").replace("`", "\\`").replace("\r", " ").replace("\n", " ")


def previous_facts(body):
    facts = {}
    for line in body.splitlines():
        columns = [value.strip() for value in re.split(r"(?<!\\)\|", line)]
        if len(columns) == 5 and re.fullmatch(r"`[a-f0-9]{64}`", columns[2]) and re.fullmatch(r"`[a-f0-9]{64}`", columns[3]):
            facts.setdefault(columns[1], {}).update(archiveSHA256=columns[2][1:-1], imageSHA256=columns[3][1:-1])
        if len(columns) == 9:
            match = re.match(r"\[([^\]]+)\]\(", columns[4])
            if match:
                facts.setdefault(match[1], {}).update(originalImageName=columns[6].replace("\\|", "|").replace("\\`", "`"))
    return facts


def notes(model, rows, tag):
    ordered = sorted(rows, key=lambda row: (row["releasedAt"], tuple(int(part) for part in re.findall(r"\d+", row["version"]))), reverse=True)
    lines = [f"# {model}", "", "官方原包，仅重命名。请选择与设备匹配的版本及组件。", ""]
    for channel, title in (("Stable", "正式版"), ("Beta", "测试版")):
        records = [row for row in ordered if row["channel"] == channel]
        if not records:
            continue
        lines += ["## " + title, ""]
        for row in records:
            component = {"Main": "键盘主控", "RF": "键盘无线模块", "Dongle": "USB 接收器"}[row["component"]]
            if row.get("assetName"):
                url = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{quote(row['assetName'])}"
                download = f"[下载 {Path(row['assetName']).suffix[1:].upper()}]({url})"
                if row["status"] != "verified":
                    download += " · **原包目标不符，勿刷入**"
            else:
                download = "暂缺官方文件"
            lines.append(f"- **{cell(row['version'])}** · {component} · {download}")
        lines.append("")
    lines += ["<details>", "<summary>原始文件名对照</summary>", "",
              "```text", "| 固件 | 版本 | 类型 | 重命名后的附件 | 官方原始包名 | 包内原始 BIN 名 | 状态 |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for row in ordered:
        if row.get("assetName"):
            # 下载主列表保持短链接，原名表供归档核对与下一轮增量检查。
            link = f"[{row['assetName']}](https://github.com/{REPOSITORY}/releases/download/{tag}/{quote(row['assetName'])})"
            archive, image = cell(row["originalArchiveName"]), cell(row["originalImageName"])
            status = "可下载" if row["status"] == "verified" else "目标不符"
        else:
            link, archive, image, status = "无文件", "官方未提供", "未取得", "缺下载链接或 SHA256"
        lines.append("| " + " | ".join([cell(row["component"]), cell(row["version"]), cell(row["channel"]), link, archive, image, status]) + " |")
    lines += ["```", "", "</details>", "", "<details>", "<summary>SHA256 校验</summary>", "", "```text",
              "| 附件 | 原包 SHA256 | 包内 BIN SHA256 |", "| --- | --- | --- |"]
    for row in ordered:
        if row.get("assetName"):
            lines.append(f"| {row['assetName']} | `{row['archiveSHA256']}` | `{row['imageSHA256']}` |")
    lines += ["```", "", "</details>", ""]
    result = "\n".join(lines)
    if len(result) > 100000:
        raise ValueError("Release body size exceeded")
    return result


def public_releases():
    result = {}
    for page in range(1, 12):
        url = f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=100&page={page}"
        headers = {"User-Agent": "NuPhy-archive", "Accept": "application/vnd.github+json",
                   "Authorization": "Bearer " + os.environ["GH_TOKEN"]}
        for attempt in range(4):
            try:
                with urlopen(Request(url, headers=headers), timeout=60) as response:
                    data = response.read(8_000_001)
                break
            except OSError:
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)
        if len(data) > 8_000_000:
            raise ValueError("GitHub response too large")
        rows = json.loads(data)
        for row in rows:
            result[row["tag_name"]] = row
        if len(rows) < 100:
            return result
    raise ValueError("Too many releases")


def release_index(releases):
    lines = [f"当前共 **{len(releases)} 个型号／布局分组**。点击型号直接下载，不必翻 Releases 分页。", ""]
    for category, title in (("he", "磁轴键盘"), ("mechanical", "机械轴键盘"), ("receiver", "USB 接收器"), ("other", "其他键盘")):
        rows = sorted((row for row in releases if row["category"] == category), key=lambda row: row["model"].casefold())
        if not rows:
            continue
        lines += ["## " + title, "", "| 型号（点击下载） | 布局 | 固件组件 | 已归档原包 |", "| --- | --- | --- | --- |"]
        for row in rows:
            layout = next((label for key, label in (("ANSI", "美式 ANSI"), ("ISO", "欧式 ISO"), ("JIS", "日式 JIS")) if re.search(r"\b" + key + r"\b", row["model"])), "型号默认布局")
            if category == "receiver":
                layout = "不适用"
            components = "、".join({"Main": "主控", "RF": "键盘无线模块", "Dongle": "接收器"}[key] for key in row["components"])
            lines.append(f"| [{cell(row['model'])}](https://github.com/{REPOSITORY}/releases/tag/{row['tag']}) | {layout} | {components} | {len(row['assets'])} |")
        lines.append("")
    return "\n".join(lines)


def update_index(readme, releases):
    start, end = "<!-- firmware-index:start -->", "<!-- firmware-index:end -->"
    if readme.count(start) != 1 or readme.count(end) != 1:
        raise ValueError("README index markers missing or repeated")
    before, rest = readme.split(start)
    _, after = rest.split(end)
    return before + start + "\n\n" + release_index(releases) + "\n" + end + after


def download(destination):
    if destination.exists():
        raise ValueError("Fresh runner directory required; no local cache accepted")
    destination.mkdir(parents=True)
    settings = config()

    def api(name, params=None):
        url = settings["api"][name] + ("?" + urlencode(params) if params else "")
        value = json.loads(get(url, settings, 2_000_000))
        if value.get("code") != 200:
            raise ValueError("Official API rejected request")
        return value.get("data")

    targets, entries = collect(api)
    existing = public_releases()
    facts = {tag: previous_facts(release.get("body") or "") for tag, release in existing.items()}
    assets = {tag: {asset["name"]: asset for asset in release["assets"]} for tag, release in existing.items()}

    def fetch(item):
        row = dict(item)
        if not row["officialURL"] or not row["imageSHA256"]:
            return {**row, "status": "unavailable", "assetName": None}
        valid_url(row["officialURL"], settings)
        mask(row["officialURL"])
        extension = Path(urlsplit(row["officialURL"]).path).suffix.lower()
        tag = "official-" + slug(row["model"]).lower()
        name = stem(row)
        original = unquote(Path(urlsplit(row["officialURL"]).path).name)
        for candidate in (name + extension, name + "_WRONG-TARGET" + extension):
            old = facts.get(tag, {}).get(candidate, {})
            asset = assets.get(tag, {}).get(candidate, {})
            if old.get("imageSHA256") == row["imageSHA256"] and old.get("originalImageName") and asset.get("digest") == "sha256:" + old.get("archiveSHA256", ""):
                return {**row, **old, "assetName": candidate, "originalArchiveName": original,
                        "archiveBytes": asset["size"], "status": "blocked-incompatible" if "_WRONG-TARGET" in candidate else "verified", "downloaded": False}
        data = get(row["officialURL"], settings, 16 * 1024 * 1024)
        inner, image = inspect_package(data, row["imageSHA256"], extension)
        blocked = row["imageSHA256"] == BLOCKED
        if row["component"] == "Dongle" and "WH80" in row["model"]:
            blocked |= image[:4] != bytes.fromhex("f5194a32") or image[6:8] != bytes.fromhex("aabb")
        if row["component"] == "RF" and "WH80" in row["model"]:
            blocked |= image[:4] != bytes.fromhex("f5194b32")
        filename = name + ("_WRONG-TARGET" if blocked else "") + extension
        path = destination / filename
        if path.exists():
            raise ValueError("Duplicate asset filename")
        path.write_bytes(data)
        return {**row, "assetName": filename, "originalArchiveName": original,
                "originalImageName": inner if extension == ".zip" else original,
                "archiveBytes": len(data), "archiveSHA256": digest(data), "imageBytes": len(image),
                "status": "blocked-incompatible" if blocked else "verified", "downloaded": True}

    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(fetch, entries))
    groups = {}
    for row in rows:
        groups.setdefault(row["modelID"], []).append(row)
    releases = []
    for identity, records in groups.items():
        model = records[0]["model"]
        tag = "official-" + slug(model).lower()
        body = notes(model, records, tag)
        filename = "notes-" + identity + ".md"
        (destination / filename).write_text(body)
        releases.append({"model": model, "tag": tag, "notes": filename, "category": records[0]["category"],
                         "components": sorted({row["component"] for row in records}),
                         "changed": body != (existing.get(tag, {}).get("body") or ""),
                         "assets": [{key: row[key] for key in ("assetName", "archiveSHA256", "downloaded")} for row in records if row.get("assetName")]})
    report = {"runID": os.environ["GITHUB_RUN_ID"], "workflowSHA": os.environ["GITHUB_SHA"], "releases": releases,
              "targets": len(targets), "versions": len(rows), "archives": sum(bool(row.get("assetName")) for row in rows),
              "downloaded": sum(row.get("downloaded", False) for row in rows), "localFirmwareInput": False,
              "unavailable": sum(row["status"] == "unavailable" for row in rows),
              "quarantined": sum(row["status"] == "blocked-incompatible" for row in rows)}
    (destination / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: report[key] for key in ("targets", "versions", "archives", "downloaded", "unavailable", "quarantined", "localFirmwareInput")}))


def publish(destination):
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise ValueError("Unexpected publication target")
    report = json.loads((destination / "report.json").read_text())
    if report["runID"] != os.environ["GITHUB_RUN_ID"] or report["workflowSHA"] != os.environ["GITHUB_SHA"]:
        raise ValueError("Runner provenance mismatch")

    def command(*args, check=True):
        return subprocess.run(["gh", "release", *args, "--repo", REPOSITORY], capture_output=True, text=True, check=check)

    for release in report["releases"]:
        tag = release["tag"]
        downloaded = [asset for asset in release["assets"] if asset["downloaded"]]
        for asset in downloaded:
            path = destination / asset["assetName"]
            if path.name != asset["assetName"] or path.is_symlink() or digest(path.read_bytes()) != asset["archiveSHA256"]:
                raise ValueError("Asset changed before publication")
        if not release["changed"] and not downloaded:
            print("Unchanged official release: " + release["model"], flush=True)
            continue
        found = command("view", tag, "--json", "tagName", check=False)
        if found.returncode:
            command("create", tag, "--draft", "--target", os.environ["GITHUB_SHA"], "--title", release["model"] + " 官方固件全版本", "--notes-file", str(destination / release["notes"]))
        for start in range(0, len(downloaded), 10):
            command("upload", tag, *[str(destination / asset["assetName"]) for asset in downloaded[start:start + 10]], "--clobber")
        command("edit", tag, "--draft=false", "--latest=false", "--notes-file", str(destination / release["notes"]))
        print("Published official release: " + release["model"], flush=True)
    readme = Path(__file__).resolve().parents[2] / "README.md"
    readme.write_text(update_index(readme.read_text(), report["releases"]))
    summary = {key: report[key] for key in ("targets", "versions", "archives", "downloaded", "unavailable", "quarantined", "localFirmwareInput")}
    print(json.dumps(summary))
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as stream:
            stream.write("## Official firmware archive\n\n```json\n" + json.dumps(summary, indent=2) + "\n```\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("download", "publish"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        (download if args.operation == "download" else publish)(args.output)
    except Exception as error:
        detail = " HTTP " + str(error.code) if isinstance(error, HTTPError) else ""
        print("Archive failed: " + type(error).__name__ + detail, file=sys.stderr)
        raise SystemExit(1)
