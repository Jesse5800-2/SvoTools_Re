#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
svo_tool.py — maimai FiNALE / teaGfx 引擎 .svo 容器 命令行工具

容器格式（实测；teaGfx 引擎体系，stevia 渲染命名空间）:
  - 文件头 : magic "AVTS" + uint32 version (实测 2..25)
  - 目录表 : 从 0x80 起，每块 0x400 字节
        +0x000  ASCII 文件名 (null 结尾) 形如 "__HmfToSvo__xxx.dds"
        +0x200  uint32 kind   (0=YABX 元数据块, 1=DDS 纹理)
        +0x204  uint32 seq
        +0x208  uint32 size
        +0x20C  uint32 offset
  - block0 的 offset = YABX 块文件偏移 -> 总块数 = (yabx_off - 0x80) / 0x400
  - DDS 块 : 文件内字节本身就是标准 DDS (128B 头 + 像素，可含 mip 链 / 16bit 无压缩)
    直接按 (off, size) 切片写出，不做任何头部重建。
  - 规律   : version == 总块数（解析自检用）

子命令:
  info     列出某个 .svo 的目录表（块类型/名称/尺寸/格式）
  extract  解包 DDS（单文件或递归目录），可选写出 manifest.json 供日后重打包
  verify   把 .svo 内解出的 DDS 与某目录里的“官方 dds”做 SHA256 对照
  pack     依据 extract -m 写出的 manifest.json 原样重拼为 .svo（可 --check 做 1:1 字节级 roundtrip 校验）

仅依赖 Python 标准库。无外部包。
"""
import os
import re
import sys
import glob
import struct
import base64
import hashlib
import argparse
import json

STRIDE = 0x400
DIR_BASE = 0x80
PREFIX_RE = re.compile(r"^__[A-Za-z]+ToSvo__")
NAME_SANITIZE = re.compile(r"[^\w.\- ]")


def parse(path):
    """解析 AVTS 目录表。返回 (ver, ents, data)。ents: list[dict]"""
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"AVTS":
        raise ValueError(f"不是 AVTS 文件（magic={data[:4]}）: {path}")
    ver = struct.unpack_from("<I", data, 4)[0]
    yabx_off = struct.unpack_from("<I", data, DIR_BASE + 0x20C)[0]
    if yabx_off <= DIR_BASE or (yabx_off - DIR_BASE) % STRIDE:
        raise ValueError(f"YABX 偏移异常 {yabx_off:#x}: {path}")
    total = (yabx_off - DIR_BASE) // STRIDE
    # 块数自检：version 应等于 total（实测恒成立，可作告警）
    ver_selfcheck = "" if ver == total else f"  (警告: version={ver} != 块数={total})"
    ents = []
    for k in range(total):
        b = DIR_BASE + k * STRIDE
        n = data.find(b"\x00", b, b + 0x200)
        name = data[b:n if n != -1 else b + 0x200].decode("ascii", "replace").strip()
        kind, seq, size, off = struct.unpack_from("<4I", data, b + 0x200)
        ents.append(dict(k=k, name=name, kind=kind, seq=seq,
                         size=size, off=off, ver_warn=ver_selfcheck))
    return ver, ents, data


def analyze_dds(blob):
    """从标准 DDS 头读取元信息（不校验）。返回 dict。"""
    if blob[:4] != b"DDS ":
        return dict(valid=False, err="missing magic")
    dwSize = struct.unpack_from("<I", blob, 4)[0]
    h, w, pitch = struct.unpack_from("<3I", blob, 12)
    mips = struct.unpack_from("<I", blob, 28)[0]
    pfSize = struct.unpack_from("<I", blob, 76)[0]
    pfFlags = struct.unpack_from("<I", blob, 80)[0]
    fourcc = blob[84:88]
    bits = struct.unpack_from("<I", blob, 88)[0]
    if pfFlags & 0x4:                      # DDPF_FOURCC
        fmt = fourcc.decode("latin1").strip("\x00 ") or "?"
    elif pfFlags & 0x40:                   # DDPF_RGB
        fmt = f"RGB{bits}" if bits else "RGB?"
    else:
        fmt = "unknown"
    return dict(valid=True, w=w, h=h, mips=mips, fmt=fmt,
                dwSize=dwSize, pfSize=pfSize, bpp=bits or None, bytes=len(blob))


def validate_dds(blob):
    """标准 DDS 校验 + mip 链数据量核算。返回警告字符串或 None。"""
    if blob[:4] != b"DDS ":
        return "缺少 DDS 魔数"
    info = analyze_dds(blob)
    if not info.get("valid"):
        return info.get("err")
    if info["dwSize"] != 124:
        return f"dwSize={info['dwSize']} (应为124)"
    if info["pfSize"] != 32:
        return f"ddspf.dwSize={info['pfSize']} (应为32)"
    w, h, mips, fmt, bits = info["w"], info["h"], info["mips"], info["fmt"], info["bpp"]
    exp = 0
    for i in range(max(1, mips)):
        ww, hh = max(1, w >> i), max(1, h >> i)
        if fmt == "DXT1":
            exp += ((ww + 3) // 4) * ((hh + 3) // 4) * 8
        elif fmt[:3] == "DXT" or fmt in ("ATI1", "ATI2", "BC4U", "BC4S", "BC5U", "BC5S"):
            exp += ((ww + 3) // 4) * ((hh + 3) // 4) * 16
        elif fmt == "DX10":
            return None  # 不核算 DX10
        else:
            exp += ww * hh * ((bits or 32) // 8)
    if exp is not None and exp + 128 != len(blob):
        return f"像素数据 {len(blob)-128:#x} != mip链预期 {exp:#x}"
    return None


def _clean_name(name):
    clean = PREFIX_RE.sub("", name)
    clean = NAME_SANITIZE.sub("_", clean).strip()
    if not clean.lower().endswith(".dds"):
        clean += ".dds"
    return clean


def collect_svos(src, recursive):
    if os.path.isfile(src):
        return [src]
    pat = "**/*.svo" if recursive else "*.svo"
    return sorted(glob.glob(os.path.join(src, pat), recursive=recursive))


def _default_out(src):
    if os.path.isfile(src):
        return os.path.join(os.path.dirname(src), "svo_extracted")
    return os.path.join(src, "svo_extracted")


def extract_one(svo, dest, manifest=False, quiet=False):
    ver, ents, data = parse(svo)
    os.makedirs(dest, exist_ok=True)
    yabx_off = struct.unpack_from("<I", data, DIR_BASE + 0x20C)[0]
    dds_offs = [e["off"] for e in ents if e["kind"] == 1]
    first_dds = min(dds_offs) if dds_offs else len(data)
    yabx_raw = data[yabx_off:first_dds]
    # 块间间隙：DDS 块按 128 字节对齐，块与块之间可能有填充字节，需原样保留才能 1:1 还原
    dds_sorted = sorted([e for e in ents if e["kind"] == 1], key=lambda e: e["off"])
    pad_map = {}
    prev_end = first_dds
    for e in dds_sorted:
        pad_map[e["k"]] = data[prev_end: e["off"]]
        prev_end = e["off"] + e["size"]
    chunks = []
    n_ok, n_warn = 0, 0
    for e in ents:
        if e["kind"] != 1:
            if not quiet:
                print(f"  blk{e['k']:2d} kind={e['kind']} 跳过(非DDS) {e['name']!r}")
            continue
        clean = _clean_name(e["name"])
        out_path = os.path.join(dest, f"{e['k']:03d}_{clean}")
        blob = data[e["off"]: e["off"] + e["size"]]
        warn = validate_dds(blob)
        with open(out_path, "wb") as fh:
            fh.write(blob)
        chunks.append(dict(k=e["k"], name=e["name"], kind=1, seq=e["seq"],
                           size=e["size"], off=e["off"],
                           pad=base64.b64encode(pad_map[e["k"]]).decode("ascii"),
                           file=os.path.basename(out_path)))
        n_ok += 1
        if warn:
            n_warn += 1
        if not quiet:
            info = analyze_dds(blob)
            flag = f"  !! {warn}" if warn else ""
            print(f"  blk{e['k']:2d} -> {os.path.basename(out_path):50s} "
                  f"{info.get('w')}x{info.get('h')} {info.get('fmt'):6s} "
                  f"mips={info.get('mips')}{flag}")
    if manifest:
        last_end = max((c["off"] + c["size"] for c in chunks), default=first_dds)
        tail_raw = data[last_end:]  # 最后一个 DDS 块之后、文件末尾之前的尾部字节
        man = dict(
            source=os.path.abspath(svo),
            version=ver,
            header_raw=base64.b64encode(data[0:DIR_BASE]).decode("ascii"),
            dir_raw=base64.b64encode(data[DIR_BASE:yabx_off]).decode("ascii"),
            yabx_raw=base64.b64encode(yabx_raw).decode("ascii"),
            tail_raw=base64.b64encode(tail_raw).decode("ascii"),
            chunks=chunks,
        )
        with open(os.path.join(dest, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(man, fh, indent=2, ensure_ascii=False)
        if not quiet:
            print(f"  -> manifest.json ({len(chunks)} 块)")
    if not quiet:
        print(f"  => 写出 {n_ok} 张 DDS" + (f"，{n_warn} 处警告" if n_warn else ""))
    return n_ok, n_warn


# ---------------- 子命令实现 ----------------

def cmd_info(args):
    for svo in collect_svos(args.path, args.recursive):
        ver, ents, _ = parse(svo)
        print("=" * 78)
        print(f"{svo}   version={ver}   块数={len(ents)}")
        print(f"{'blk':>3} {'kind':>4} {'seq':>4} {'size':>10} {'offset':>10}  "
              f"{'dim':>11} {'fmt':>6} {'mip':>3}  name")
        for e in ents:
            if e["kind"] == 1:
                blob = None
                info = analyze_dds(
                    open(svo, "rb").read()[e["off"]: e["off"] + min(e["size"], 128)])
                dim = f"{info.get('w')}x{info.get('h')}"
                fmt = info.get("fmt")
                mip = info.get("mips")
            else:
                dim = fmt = mip = "-"
            print(f"{e['k']:>3} {e['kind']:>4} {e['seq']:>4} {e['size']:>#10x} "
                  f"{e['off']:>#10x}  {str(dim):>11} {str(fmt):>6} {str(mip):>3}  "
                  f"{e['name']!r}{e['ver_warn']}")


def cmd_extract(args):
    out_root = args.out or _default_out(args.path)
    svos = collect_svos(args.path, args.recursive)
    if not svos:
        print(f"未找到 .svo 文件: {args.path}")
        return 1
    tot_ok = tot_warn = 0
    for svo in svos:
        if os.path.isfile(args.path):
            dest = out_root
        else:
            rel = os.path.relpath(svo, args.path)[:-4]  # 去掉 .svo
            dest = os.path.join(out_root, rel)
        if not args.quiet:
            print(f"[v{parse(svo)[0]:>2}] {svo}")
        ok, warn = extract_one(svo, dest, manifest=args.manifest, quiet=args.quiet)
        tot_ok += ok
        tot_warn += warn
    print(f"\nTOTAL: {tot_ok} 张 DDS（来自 {len(svos)} 个 svo，{tot_warn} 处警告）"
          f" -> {out_root}")
    return 0


def cmd_verify(args):
    ver, ents, data = parse(args.svo)
    # 官方 dds 文件名 -> 路径
    official = {}
    for fn in os.listdir(args.dds_dir):
        if fn.lower().endswith(".dds"):
            official.setdefault(fn.lower(), os.path.join(args.dds_dir, fn))
    matched = mismatched = missing = 0
    print(f"对照 {args.svo}  (version={ver})  vs  {args.dds_dir}")
    for e in ents:
        if e["kind"] != 1:
            continue
        clean = _clean_name(e["name"])
        blob = data[e["off"]: e["off"] + e["size"]]
        h = hashlib.sha256(blob).hexdigest()
        path = official.get(clean.lower())
        if not path:
            print(f"  [svo有/官方无] {clean}")
            missing += 1
            continue
        oh = hashlib.sha256(open(path, "rb").read()).hexdigest()
        if h == oh:
            matched += 1
            if not args.quiet:
                print(f"  [一致] {clean}")
        else:
            mismatched += 1
            print(f"  [不一致] {clean}")
    # 官方有但 svo 没有
    svo_names = {_clean_name(e["name"]).lower() for e in ents if e["kind"] == 1}
    for fn in official:
        if fn not in svo_names:
            print(f"  [官方有/svo无] {fn}")
            missing += 1
    print(f"\n结果: 一致={matched}  不一致={mismatched}  缺失(任一侧)={missing}")
    return 0 if mismatched == 0 else 2


def _default_pack_out(source, mdir):
    if source:
        base, ext = os.path.splitext(source)
        return base + ".repacked" + (ext or ".svo")
    return mdir.rstrip(os.sep) + ".repacked.svo"


def repack(mdir, manifest, out_path, check=False):
    """依据 manifest 原样重拼 SVO：header + dir_raw + yabx_raw + DDS 块（按原偏移排序）。"""
    header = base64.b64decode(manifest["header_raw"])
    directory = base64.b64decode(manifest["dir_raw"])
    yabx = base64.b64decode(manifest["yabx_raw"])
    chunks = sorted(manifest.get("chunks", []), key=lambda c: c["off"])
    out = bytearray()
    out += header
    out += directory
    out += yabx
    warnings = []
    for c in chunks:
        pad = base64.b64decode(c.get("pad", ""))
        if pad:
            out += pad
        fp = os.path.join(mdir, c["file"])
        if not os.path.isfile(fp):
            warnings.append(f"缺失文件: {c['file']}")
            continue
        blob = open(fp, "rb").read()
        if len(blob) != c["size"]:
            warnings.append(f"{c['file']} 大小 {len(blob)} != manifest {c['size']}"
                            f"（尺寸变化需重建目录，当前为原样拼接）")
        if blob[:4] != b"DDS ":
            warnings.append(f"{c['file']} 缺少 DDS 魔数")
        out += blob
    out += base64.b64decode(manifest.get("tail_raw", ""))  # 末尾尾部字节
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(out)
    if check and manifest.get("source") and os.path.isfile(manifest["source"]):
        orig = open(manifest["source"], "rb").read()
        if bytes(out) == orig:
            print(f"  ROUNDTRIP ✓ 重建与原始逐字节一致 ({len(out)} 字节)")
        else:
            print(f"  ROUNDTRIP ✗ 不一致: 重建 {len(out)} 字节 vs 原 {len(orig)} 字节")
    return warnings


def cmd_pack(args):
    mdir = args.dir
    mpath = os.path.join(mdir, "manifest.json")
    if not os.path.isfile(mpath):
        print(f"错误: 未找到 {mpath}（请先用 extract -m 生成 manifest）", file=sys.stderr)
        return 1
    manifest = json.load(open(mpath, encoding="utf-8"))
    out = args.out or _default_pack_out(manifest.get("source"), mdir)
    print(f"重打包: {mdir}\n  -> {out}")
    warnings = repack(mdir, manifest, out, check=args.check)
    n = len(manifest.get("chunks", []))
    for w in warnings:
        print(f"  !! {w}")
    print(f"  => 写出 {n} 块" + (f"，{len(warnings)} 处警告" if warnings else ""))
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="svo_tool.py",
        description="maimai FiNALE / teaGfx 引擎 .svo 容器工具 (extract / info / verify / pack)")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("info", help="列出 .svo 的目录表与纹理信息")
    pi.add_argument("path", help=".svo 文件或目录")
    pi.add_argument("-r", "--recursive", action="store_true", help="目录递归")
    pi.set_defaults(func=cmd_info)

    pe = sub.add_parser("extract", help="解包 DDS（支持递归/目录）")
    pe.add_argument("path", help=".svo 文件或目录")
    pe.add_argument("-o", "--out", help="输出根目录（默认 <源>/svo_extracted）")
    pe.add_argument("-r", "--recursive", action="store_true", help="目录递归")
    pe.add_argument("-m", "--manifest", action="store_true",
                    help="同时写出 manifest.json（含原始头部/目录/YABX，供日后重打包）")
    pe.add_argument("-q", "--quiet", action="store_true", help="仅打印汇总")
    pe.set_defaults(func=cmd_extract)

    pv = sub.add_parser("verify", help="与官方 dds 目录做 SHA256 对照")
    pv.add_argument("svo", help=".svo 文件")
    pv.add_argument("dds_dir", help="含官方 dds 的目录")
    pv.add_argument("-q", "--quiet", action="store_true", help="只统计，不逐条列出一致项")
    pv.set_defaults(func=cmd_verify)

    pp = sub.add_parser("pack", help="依据 extract -m 生成的目录重打包为 .svo")
    pp.add_argument("dir", help="含 manifest.json 与 DDS 的抽取目录")
    pp.add_argument("-o", "--out", help="输出 .svo（默认 <源>.repacked.svo）")
    pp.add_argument("-c", "--check", action="store_true",
                    help="与原始 .svo 做 1:1 字节级对照（roundtrip 校验）")
    pp.set_defaults(func=cmd_pack)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError) as ex:
        print(f"错误: {ex}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
