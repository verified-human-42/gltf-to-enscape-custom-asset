"""GLTF to Enscape Custom Asset - drag-and-drop GUI around Enscape's own batch importer.

Drop .gltf / .glb files, or folders containing them, onto the window, then Import to Enscape Custom Assets.
Titles already in the Output Directory are imported again (Enscape gives each import its own id).
Terms follow Enscape's Custom Asset Editor: Project Directory (.assetpkg), Output Directory (exported assets).
Per asset:
 1. stage a copy named "<Title>.gltf" (the importer takes the asset name from the file name) holding only the glTF and the
    files it references (.glb is unpacked to .gltf + .bin; the importer only scans *.gltf)
 2. fix metallic: glTF's default metallicFactor 1 makes Enscape render fabric/wood as metal, so
    metallic = factor x mean of the metallicRoughness texture's BLUE channel (factor as-is when there is no texture)
 3. run "C:\\Program Files\\Enscape\\RendererHost\\Enscape.CustomAssetBatchImporter.exe <staging dir>"
    -> exported asset (Output Directory) + editable .assetpkg (Project Directory), both read from
       %LOCALAPPDATA%\\Enscape\\CustomAssetEditorSettings.json. It prints "<gltf>;<guid>;True" on success, then crashes on
       shutdown (harmless; ignored).
 4. write the description into the exported asset's JSON and the .assetpkg.
Ported from tools/enscape_custom_assets.py (Revit - Revit MCP), verified with Enscape 4.7.
"""
import glob
import io
import json
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import urllib.request
import zipfile
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageDraw, ImageStat, ImageTk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:
    TkinterDnD = None

APP = "GLTF to Enscape Custom Asset"
IMPORTER = r"C:\Program Files\Enscape\RendererHost\Enscape.CustomAssetBatchImporter.exe"
SETTINGS = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Enscape", "CustomAssetEditorSettings.json")
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW


# ---------------------------------------------------------------- core

def library_dirs():
    s = json.load(open(SETTINGS, encoding="utf-8-sig"))
    return s["ProjectDirectory"], s["OutputDirectory"]


def existing_titles(out_dir):
    titles = {}
    for j in glob.glob(os.path.join(out_dir, "*", "*.json")):
        try:
            titles[json.load(open(j, encoding="utf-8-sig"))["Name"]] = j
        except Exception:
            pass
    return titles


def find_models(path):
    """A dropped path -> list of .gltf/.glb files (a folder is searched recursively)."""
    if os.path.isfile(path):
        return [path] if path.lower().endswith((".gltf", ".glb")) else []
    found = []
    for root, _, files in os.walk(path):
        found += [os.path.join(root, f) for f in files if f.lower().endswith((".gltf", ".glb"))]
    return sorted(found)


def default_title(model):
    """File name without a trailing texture-resolution tag; <name>/<name>_2k.gltf -> "name"."""
    stem = os.path.splitext(os.path.basename(model))[0]
    stem = re.sub(r"[_ -]\d+k$", "", stem, flags=re.I)
    return stem.replace("_", " ").strip() or "model"


def read_glb(path):
    data = open(path, "rb").read()
    magic, _, length = struct.unpack_from("<III", data, 0)
    if magic != 0x46546C67:
        raise ValueError("not a GLB file")
    pos, js, binary = 12, None, b""
    while pos < length:
        clen, ctype = struct.unpack_from("<II", data, pos)
        chunk = data[pos + 8:pos + 8 + clen]
        if ctype == 0x4E4F534A:
            js = json.loads(chunk.decode("utf-8"))
        elif ctype == 0x004E4942:
            binary = chunk
        pos += 8 + clen
    return js, binary


def image_bytes(g, img, base, binary):
    """Raw bytes of a glTF image: file uri, data uri or bufferView."""
    if "bufferView" in img:
        bv = g["bufferViews"][img["bufferView"]]
        i = bv.get("buffer", 0)
        src = binary if binary and i == 0 else open(os.path.join(base, urllib.request.unquote(g["buffers"][i]["uri"])), "rb").read()
        off = bv.get("byteOffset", 0)
        return src[off:off + bv["byteLength"]]
    uri = img["uri"]
    if uri.startswith("data:"):
        import base64
        return base64.b64decode(uri.split(",", 1)[1])
    return open(os.path.join(base, urllib.request.unquote(uri)), "rb").read()


def stage(model, title, root):
    """Copy the model into <root>/<title>/<title>.gltf with only its referenced files, and fix metallic values."""
    dst = os.path.join(root, title)
    os.makedirs(dst)
    base = os.path.dirname(model)
    if model.lower().endswith(".glb"):
        g, binary = read_glb(model)
        if g.get("buffers") and "uri" not in g["buffers"][0]:
            open(os.path.join(dst, title + ".bin"), "wb").write(binary)
            g["buffers"][0]["uri"] = title + ".bin"
    else:
        g, binary = json.load(open(model, encoding="utf-8")), b""
    for item in g.get("buffers", []) + g.get("images", []):
        uri = item.get("uri", "")
        if uri and not uri.startswith("data:") and not (binary and uri == title + ".bin"):
            src = os.path.join(base, urllib.request.unquote(uri))
            out = os.path.join(dst, urllib.request.unquote(uri))
            os.makedirs(os.path.dirname(out), exist_ok=True)
            shutil.copy2(src, out)
    # embedded images (bufferView / data uri) -> plain texture files next to the glTF
    for n, img in enumerate(g.get("images", [])):
        if "bufferView" in img or img.get("uri", "").startswith("data:"):
            ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(
                img.get("mimeType") or img.get("uri", "")[5:].split(";")[0], ".png")
            rel = "textures/%s_%d%s" % (title, n, ext)
            os.makedirs(os.path.join(dst, "textures"), exist_ok=True)
            open(os.path.join(dst, rel), "wb").write(image_bytes(g, img, base, binary))
            img.pop("bufferView", None)
            img.pop("mimeType", None)
            img["uri"] = rel
    report = []
    for m in g.get("materials", []):
        pbr = m.setdefault("pbrMetallicRoughness", {})
        factor = pbr.get("metallicFactor", 1.0)
        tex = pbr.get("metallicRoughnessTexture")
        if tex is not None:
            try:
                img = g["images"][g["textures"][tex["index"]]["source"]]
                blue = Image.open(io.BytesIO(image_bytes(g, img, dst, binary))).convert("RGB").split()[2]
                factor *= ImageStat.Stat(blue).mean[0] / 255.0
            except Exception as e:
                report.append("%s: metallic texture unreadable (%s)" % (m.get("name", "?"), e))
        pbr["metallicFactor"] = round(factor, 2)
        report.append("%s %.2f" % (m.get("name", "?"), pbr["metallicFactor"]))
    json.dump(g, open(os.path.join(dst, title + ".gltf"), "w", encoding="utf-8"))
    return dst, report


def set_description(project_dir, out_dir, guid, description):
    for j in glob.glob(os.path.join(out_dir, "*_" + guid, guid + ".json")):
        d = json.load(open(j, encoding="utf-8-sig"))
        d["Description"] = description
        json.dump(d, open(j, "w", encoding="utf-8"), indent=2)
    for pkg in glob.glob(os.path.join(project_dir, "*_" + guid + ".assetpkg")):
        tmp = pkg + ".tmp"
        with zipfile.ZipFile(pkg) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == "Attachments/MetaInfo.json":
                    meta = json.loads(data.decode("utf-8-sig"))
                    meta["Description"] = description
                    data = json.dumps(meta).encode("utf-8")
                zout.writestr(item, data)
        os.replace(tmp, pkg)


def import_one(model, title, description, project_dir, out_dir):
    """-> (guid, material report). Raises on failure."""
    root = tempfile.mkdtemp(prefix="enscape_import_")
    try:
        sdir, report = stage(model, title, root)
        r = subprocess.run([IMPORTER, sdir], capture_output=True, text=True, errors="replace", timeout=900,
                           creationflags=NO_WINDOW)
        ok = [l.split(";") for l in r.stdout.splitlines() if l.count(";") == 2]
        if not ok or ok[0][2].strip() != "True":
            raise RuntimeError("importer failed (exit %s)\n%s\n%s" % (r.returncode, r.stdout[-1500:], r.stderr[-1500:]))
        guid = ok[0][1]
        if description:
            set_description(project_dir, out_dir, guid, description)
        return guid, report
    finally:
        shutil.rmtree(root, ignore_errors=True)




def load_gltf(model):
    """-> (json, glb binary or b'')."""
    if model.lower().endswith(".glb"):
        return read_glb(model)
    return json.load(open(model, encoding="utf-8")), b""


def model_info(model):
    """Size, triangle count, material count and a base-colour thumbnail (PIL image or None) for the list."""
    g, binary = load_gltf(model)
    base = os.path.dirname(model)
    size = os.path.getsize(model)
    if not binary:
        for item in g.get("buffers", []) + g.get("images", []):
            uri = item.get("uri", "")
            if uri and not uri.startswith("data:"):
                try:
                    size += os.path.getsize(os.path.join(base, urllib.request.unquote(uri)))
                except OSError:
                    pass
    tris = 0
    for mesh in g.get("meshes", []):
        for prim in mesh.get("primitives", []):
            if prim.get("mode", 4) != 4:
                continue
            acc = prim.get("indices", prim.get("attributes", {}).get("POSITION"))
            if acc is not None:
                tris += g["accessors"][acc]["count"] // 3
    thumb = None
    for m in g.get("materials", []):
        tex = m.get("pbrMetallicRoughness", {}).get("baseColorTexture")
        if tex is None:
            continue
        try:
            img = g["images"][g["textures"][tex["index"]]["source"]]
            thumb = Image.open(io.BytesIO(image_bytes(g, img, base, binary)))
            thumb.draft("RGB", (256, 256))
            thumb = thumb.convert("RGB")
            break
        except Exception:
            thumb = None
    if thumb is None and g.get("materials"):
        f = g["materials"][0].get("pbrMetallicRoughness", {}).get("baseColorFactor", [0.6, 0.6, 0.6, 1])
        thumb = Image.new("RGB", (8, 8), tuple(int(255 * min(1, max(0, c)) ** (1 / 2.2)) for c in f[:3]))
    return {"size": size, "tris": tris, "materials": len(g.get("materials", [])), "thumb": thumb}


def library_preview(json_path):
    import base64
    d = json.load(open(json_path, encoding="utf-8-sig"))
    im = Image.open(io.BytesIO(base64.b64decode(d["PreviewImage"]))).convert("RGB")
    # Enscape renders the asset small in a big empty frame: crop to the object, square, with a margin
    from PIL import ImageChops
    diff = ImageChops.difference(im, Image.new("RGB", im.size, im.getpixel((2, 2)))).convert("L")
    box = diff.point(lambda v: 255 if v > 18 else 0).getbbox()
    if box:
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        half = max(box[2] - box[0], box[3] - box[1]) * 0.58
        out = Image.new("RGB", (int(2 * half), int(2 * half)), im.getpixel((2, 2)))
        out.paste(im, (int(half - cx), int(half - cy)))
        im = out
    return im


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1024.0


# ---------------------------------------------------------------- preview render (pure Python, flat-shaded)

_CTYPES = {5120: "b", 5121: "B", 5122: "h", 5123: "H", 5125: "I", 5126: "f"}
_NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def _buffer_data(g, i, base, binary, cache):
    if i not in cache:
        uri = g["buffers"][i].get("uri")
        if uri is None:
            cache[i] = binary
        elif uri.startswith("data:"):
            import base64
            cache[i] = base64.b64decode(uri.split(",", 1)[1])
        else:
            cache[i] = open(os.path.join(base, urllib.request.unquote(uri)), "rb").read()
    return cache[i]


def _accessor(g, idx, base, binary, cache):
    import array
    a = g["accessors"][idx]
    bv = g["bufferViews"][a["bufferView"]]
    data = _buffer_data(g, bv.get("buffer", 0), base, binary, cache)
    fmt, n, count = _CTYPES[a["componentType"]], _NCOMP[a["type"]], a["count"]
    size = struct.calcsize(fmt)
    start = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
    stride = bv.get("byteStride") or size * n
    if stride == size * n:
        arr = array.array(fmt)
        arr.frombytes(data[start:start + count * n * size])
        return arr, n
    out = array.array(fmt)
    for k in range(count):
        out.frombytes(data[start + k * stride:start + k * stride + n * size])
    return out, n


def _matmul(a, b):
    return [sum(a[r + 4 * k] * b[k + 4 * c] for k in range(4)) for c in range(4) for r in range(4)]  # column-major


def _node_matrix(n):
    if "matrix" in n:
        return list(n["matrix"])
    tx, ty, tz = n.get("translation", [0, 0, 0])
    x, y, z, w = n.get("rotation", [0, 0, 0, 1])
    sx, sy, sz = n.get("scale", [1, 1, 1])
    r = [1 - 2 * (y * y + z * z), 2 * (x * y + z * w), 2 * (x * z - y * w),
         2 * (x * y - z * w), 1 - 2 * (x * x + z * z), 2 * (y * z + x * w),
         2 * (x * z + y * w), 2 * (y * z - x * w), 1 - 2 * (x * x + y * y)]
    return [r[0] * sx, r[1] * sx, r[2] * sx, 0, r[3] * sy, r[4] * sy, r[5] * sy, 0,
            r[6] * sz, r[7] * sz, r[8] * sz, 0, tx, ty, tz, 1]


def _material_colors(g, base, binary):
    cols = []
    for m in g.get("materials", []):
        pbr = m.get("pbrMetallicRoughness", {})
        f = pbr.get("baseColorFactor", [1, 1, 1, 1])
        c = [1.0, 1.0, 1.0]
        tex = pbr.get("baseColorTexture")
        if tex is not None:
            try:
                img = g["images"][g["textures"][tex["index"]]["source"]]
                im = Image.open(io.BytesIO(image_bytes(g, img, base, binary)))
                im.draft("RGB", (128, 128))
                c = [v / 255.0 for v in ImageStat.Stat(im.convert("RGB").resize((64, 64))).mean]
            except Exception:
                c = [0.7, 0.7, 0.7]
        cols.append([c[i] * f[i] for i in range(3)])
    return cols


def render_preview(model, size=256, max_tris=60000):
    """Small flat-shaded three-quarter view of the model (RGBA), or None."""
    import math
    g, binary = load_gltf(model)
    base, cache = os.path.dirname(model), {}
    mats = _material_colors(g, base, binary)
    scene = g.get("scenes", [{}])[g.get("scene", 0)] if g.get("scenes") else {"nodes": list(range(len(g.get("nodes", []))))}
    tris = []  # (3 world points, colour)
    total = sum(g["accessors"][p.get("indices", p["attributes"]["POSITION"])]["count"] // 3
                for m in g.get("meshes", []) for p in m.get("primitives", []) if "POSITION" in p.get("attributes", {}))
    step = max(1, total // max_tris)

    def walk(ni, parent):
        n = g["nodes"][ni]
        mtx = _matmul(parent, _node_matrix(n))
        if "mesh" in n:
            for p in g["meshes"][n["mesh"]].get("primitives", []):
                if p.get("mode", 4) != 4 or "POSITION" not in p.get("attributes", {}):
                    continue
                pos, _ = _accessor(g, p["attributes"]["POSITION"], base, binary, cache)
                pts = [(mtx[0] * pos[i] + mtx[4] * pos[i + 1] + mtx[8] * pos[i + 2] + mtx[12],
                        mtx[1] * pos[i] + mtx[5] * pos[i + 1] + mtx[9] * pos[i + 2] + mtx[13],
                        mtx[2] * pos[i] + mtx[6] * pos[i + 1] + mtx[10] * pos[i + 2] + mtx[14])
                       for i in range(0, len(pos), 3)]
                if "indices" in p:
                    idx, _ = _accessor(g, p["indices"], base, binary, cache)
                else:
                    idx = range(len(pts))
                col = mats[p["material"]] if "material" in p and p["material"] < len(mats) else [0.7, 0.7, 0.7]
                for t in range(0, len(idx) - 2, 3 * step):
                    tris.append(((pts[idx[t]], pts[idx[t + 1]], pts[idx[t + 2]]), col))
        for c in n.get("children", []):
            walk(c, mtx)

    for ni in scene.get("nodes", []):
        walk(ni, [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1])
    if not tris:
        return None
    # view: yaw 35 deg, pitch 25 deg (glTF is Y-up)
    ya, pa = math.radians(-35), math.radians(25)
    cy, sy, cp, sp = math.cos(ya), math.sin(ya), math.cos(pa), math.sin(pa)

    def view(p):
        x, y, z = p
        x, z = x * cy + z * sy, -x * sy + z * cy
        y, z = y * cp - z * sp, y * sp + z * cp
        return x, y, z
    vt = [([view(p) for p in t], c) for t, c in tris]
    xs = [p[0] for t, _ in vt for p in t]
    ys = [p[1] for t, _ in vt for p in t]
    span = max(max(xs) - min(xs), max(ys) - min(ys)) or 1.0
    ss = size * 3
    sc, ox, oy = ss * 0.86 / span, (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
    light = (-0.45, 0.75, 0.5)
    ln = math.sqrt(sum(v * v for v in light))
    light = [v / ln for v in light]
    faces = []
    for (a, b, c), col in vt:
        ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        nl = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
        nx, ny, nz = nx / nl, ny / nl, nz / nl
        if nz < 0:  # two-sided: face the camera
            nx, ny, nz = -nx, -ny, -nz
        shade = 0.38 + 0.62 * max(0.0, nx * light[0] + ny * light[1] + nz * light[2])
        rgb = tuple(int(255 * min(1.0, (col[i] ** (1 / 2.2)) * shade)) for i in range(3))
        pts = [(ss / 2 + (p[0] - ox) * sc, ss / 2 - (p[1] - oy) * sc) for p in (a, b, c)]
        faces.append((a[2] + b[2] + c[2], pts, rgb))
    faces.sort(key=lambda f: f[0])  # painter's: far (small z) first, camera looks down -z
    im = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for _, pts, rgb in faces:
        d.polygon(pts, fill=rgb)
    return im.resize((size, size), Image.LANCZOS)


# ---------------------------------------------------------------- GUI

try:
    import sv_ttk
except ImportError:
    sv_ttk = None

GO = "Import to Enscape Custom Assets"
DOT = "  ·  "
STATUS = {  # status -> (label, tag)
    "queued": ("Ready", "queued"),
    "library": ("Ready (already present)", "queued"),
    "importing": ("Importing...", "busy"),
    "done": ("\u2713  Imported", "done"),
    "failed": ("\u2715  Failed", "failed"),
    "cancelled": ("Cancelled", "skipped"),
}


def windows_dark_mode():
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except Exception:
        return False


EDITOR = os.path.join(os.path.dirname(IMPORTER), "Enscape.CustomAssetEditor.exe")
HOW_TO = (
    ("Click  Open Enscape Custom Asset Editor  below.", ""),
    ("In the editor, click  Configure  (top right).", ""),
    ("Step 1 - Project Directory:  leave it as it is and click  Next.", ""),
    ("Step 2 - Output Directory:  click the blue folder path and choose the folder you want, "
     "then click  Save and Close.",
     "It can't be the same folder as the Project Directory, or inside it."),
    ("In Revit, open  Enscape > Asset Library > Custom Assets,  click the folder icon (bottom right) "
     "and choose the same folder.",
     "If these two folders don't match, Enscape shows no custom assets."),
)


class HowToDialog(tk.Toplevel):
    """'How to change?' - plain steps for changing Enscape's Output Directory."""
    def __init__(self, app):
        super().__init__(app.root)
        self.title("How to change the output directory")
        self.transient(app.root)
        self.resizable(False, False)
        f = ttk.Frame(self, padding=app.px(20))
        f.pack(fill="both", expand=True)
        wrap = app.px(520)
        ttk.Label(f, text="Change where your assets go", font=("Segoe UI Semibold", 13)).pack(anchor="w")
        ttk.Label(f, text="The folder is set in Enscape, not in this app. This app always uses Enscape's Output Directory.",
                  style="Dim.TLabel", wraplength=wrap).pack(anchor="w", pady=(app.px(4), app.px(14)))
        for n, (step, note) in enumerate(HOW_TO, 1):
            row = ttk.Frame(f)
            row.pack(fill="x", pady=(0, app.px(10)))
            ttk.Label(row, text="%d" % n, width=3, font=("Segoe UI Semibold", 11),
                      foreground=app.colors["accent"]).pack(side="left", anchor="n")
            col = ttk.Frame(row)
            col.pack(side="left", fill="x")
            ttk.Label(col, text=step, wraplength=wrap).pack(anchor="w")
            if note:
                ttk.Label(col, text=note, style="Dim.TLabel", wraplength=wrap).pack(anchor="w")
        ttk.Label(f, text="This app picks up the new folder by itself once you're done.", style="Dim.TLabel",
                  wraplength=wrap).pack(anchor="w", pady=(app.px(4), 0))
        b = ttk.Frame(f)
        b.pack(fill="x", pady=(app.px(16), 0))
        ttk.Button(b, text="Close", command=self.destroy).pack(side="right")
        ttk.Button(b, text="Open Enscape Custom Asset Editor", style="Accent.TButton",
                   command=self.open_editor).pack(side="right", padx=app.px(8))
        self.bind("<Escape>", lambda e: self.destroy())
        app.dark_titlebar(self)
        self.update_idletasks()
        x = app.root.winfo_rootx() + (app.root.winfo_width() - self.winfo_width()) // 2
        y = app.root.winfo_rooty() + (app.root.winfo_height() - self.winfo_height()) // 3
        self.geometry("+%d+%d" % (x, y))

    def open_editor(self):
        try:
            subprocess.Popen([EDITOR])
        except OSError:
            messagebox.showerror(APP, "Couldn't start the Enscape Custom Asset Editor. Open it from the Windows "
                                      "Start menu instead.", parent=self)


class EditDialog(tk.Toplevel):
    def __init__(self, app, iid):
        super().__init__(app.root)
        self.app, self.iid = app, iid
        it = app.items[iid]
        self.title("Edit asset")
        self.transient(app.root)
        self.resizable(True, False)
        f = ttk.Frame(self, padding=app.px(16))
        f.pack(fill="both", expand=True)
        f.columnconfigure(0, weight=1)
        ttk.Label(f, text="Title (asset name in Enscape)").grid(row=0, column=0, sticky="w")
        self.t = ttk.Entry(f, width=60)
        self.t.insert(0, it["title"])
        self.t.grid(row=1, column=0, sticky="ew", pady=(4, 12))
        ttk.Label(f, text="Description").grid(row=2, column=0, sticky="w")
        c = app.colors
        self.d = tk.Text(f, height=4, width=60, wrap="word", relief="flat", font=("Segoe UI", 10),
                         bg=c["field"], fg=c["fg"], insertbackground=c["fg"],
                         highlightthickness=1, highlightbackground=c["border"], padx=6, pady=6)
        self.d.insert("1.0", it["desc"])
        self.d.grid(row=3, column=0, sticky="ew", pady=(4, 12))
        ttk.Label(f, text=it["model"], style="Dim.TLabel", wraplength=app.px(460)).grid(row=4, column=0, sticky="w")
        b = ttk.Frame(f)
        b.grid(row=5, column=0, sticky="e", pady=(app.px(16), 0))
        ttk.Button(b, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(b, text="Save", style="Accent.TButton", command=self.save).pack(side="right", padx=8)
        self.t.bind("<Return>", lambda e: self.save())
        self.bind("<Escape>", lambda e: self.destroy())
        app.dark_titlebar(self)
        self.update_idletasks()
        x = app.root.winfo_rootx() + (app.root.winfo_width() - self.winfo_width()) // 2
        y = app.root.winfo_rooty() + (app.root.winfo_height() - self.winfo_height()) // 3
        self.geometry("+%d+%d" % (x, y))
        self.t.focus_set()
        self.t.select_range(0, "end")
        self.grab_set()

    def save(self):
        title = "".join(c for c in self.t.get().strip() if c not in '<>:"/\\|?*;')
        if not title:
            return
        it = self.app.items[self.iid]
        it["title"], it["desc"] = title, self.d.get("1.0", "end").strip()
        self.app.refresh_row(self.iid)
        self.app.check_library(self.iid)
        self.destroy()


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.busy = False
        self.cancel = False
        self.items = {}  # iid -> dict(model, title, desc, status, note)
        self.photos = {}  # iid -> PhotoImage (kept alive)
        self.library = {}  # title -> library json path
        self.k = root.winfo_fpixels("1i") / 96.0
        self.dark = {"dark": True, "light": False}.get(os.environ.get("GLTF2ENSCAPE_THEME"), windows_dark_mode())
        if sv_ttk:
            sv_ttk.set_theme("dark" if self.dark else "light")
        self.colors = ({"bg": "#1c1c1c", "fg": "#fafafa", "dim": "#9a9a9a", "field": "#2a2a2a", "border": "#454545",
                        "accent": "#57c8ff", "ok": "#6ccb5f", "err": "#ff99a4", "busy": "#57c8ff", "zone": "#202020", "tile": "#3a3a3a"}
                       if self.dark else
                       {"bg": "#fafafa", "fg": "#1c1c1c", "dim": "#6b6b6b", "field": "#ffffff", "border": "#cfcfcf",
                        "accent": "#005fb8", "ok": "#0f7b0f", "err": "#c42b1c", "busy": "#005fb8", "zone": "#f3f3f3", "tile": "#eeeeee"})
        self.thumb_px = self.px(48)

        root.title(APP)
        root.geometry("%dx%d" % (self.px(1100), self.px(720)))
        root.minsize(self.px(860), self.px(540))
        root.configure(bg=self.colors["bg"])
        try:
            root.iconbitmap(default=resource("icon.ico"))
        except Exception:
            pass
        self.blank = ImageTk.PhotoImage(self.tile())
        self.styles()
        self.build()
        self.dark_titlebar(root)
        self.load_library()
        self.update_footer()
        root.after(80, self.poll)

    # ---------------------------------------------------------------- layout
    def px(self, v):
        return int(round(v * self.k))

    def styles(self):
        st = ttk.Style()
        c = self.colors
        st.configure("Title.TLabel", font=("Segoe UI Variable Display", 17, "bold"))
        st.configure("Dim.TLabel", foreground=c["dim"], font=("Segoe UI", 9))
        st.configure("Treeview", rowheight=self.thumb_px + self.px(12), font=("Segoe UI", 10))
        st.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))

    def build(self):
        c, root = self.colors, self.root
        pad = self.px(16)

        # header
        head = ttk.Frame(root, padding=(pad, pad, pad, self.px(12)))
        head.pack(fill="x")
        tb = ttk.Frame(head)
        tb.pack(side="left", fill="x", expand=True)
        ttk.Label(tb, text="GLTF → Enscape Custom Asset", style="Title.TLabel").pack(anchor="w")
        lr = ttk.Frame(tb)
        lr.pack(anchor="w", pady=(self.px(2), 0))
        self.lib_label = ttk.Label(lr, text="", style="Dim.TLabel")
        self.lib_label.pack(side="left")
        how = ttk.Label(lr, text="How to change?", foreground=self.colors["accent"], cursor="hand2",
                        font=("Segoe UI", 9, "underline"))
        how.pack(side="left", padx=(self.px(10), 0))
        how.bind("<Button-1>", lambda e: HowToDialog(self))
        # re-read Enscape's settings whenever the window comes back to the front (e.g. after changing them)
        root.bind("<FocusIn>", lambda e: self.on_focus() if e.widget is root else None)

        # toolbar
        bar = ttk.Frame(root, padding=(pad, 0, pad, self.px(10)))
        bar.pack(fill="x")
        ttk.Button(bar, text="+  Add files", command=self.pick_files).pack(side="left")
        ttk.Button(bar, text="+  Add folder", command=self.pick_folder).pack(side="left", padx=self.px(6))
        ttk.Button(bar, text="Clear list", command=self.clear).pack(side="right")

        # footer (packed before the list so it keeps its height): progress row, then the log (always shown)
        foot = ttk.Frame(root, padding=(pad, self.px(10), pad, pad))
        foot.pack(side="bottom", fill="x")
        prog = ttk.Frame(foot)
        prog.pack(fill="x")
        self.go = ttk.Button(prog, text=GO, style="Accent.TButton", command=self.start, width=32)
        self.go.pack(side="right")
        self.status = ttk.Label(prog, text="")
        self.status.pack(side="left")
        self.pbar = ttk.Progressbar(prog, mode="determinate")
        self.pbar.pack(side="right", fill="x", expand=True, padx=self.px(16))
        self.logf = ttk.Frame(foot)
        self.logf.pack(fill="both", pady=(self.px(10), 0))
        self.log = tk.Text(self.logf, height=7, font=("Cascadia Mono", 9), state="disabled", wrap="word",
                           relief="flat", bg=c["field"], fg=c["fg"], padx=8, pady=6, highlightthickness=1,
                           highlightbackground=c["border"])
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure("err", foreground=c["err"])
        self.log.tag_configure("ok", foreground=c["ok"])

        # list
        mid = ttk.Frame(root, padding=(pad, 0))
        mid.pack(fill="both", expand=True)
        cols = ("title", "status", "info", "source")
        self.tree = ttk.Treeview(mid, columns=cols, show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="")
        self.tree.column("#0", width=self.thumb_px + self.px(30), stretch=False, anchor="center")
        for col, text, w, st in (("title", "Asset", 230, True), ("status", "Status", 150, False),
                                 ("info", "Details", 260, False), ("source", "Source", 280, True)):
            self.tree.heading(col, text=text, anchor="w")
            self.tree.column(col, width=self.px(w), stretch=st, anchor="w")
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        for tag, col in (("failed", c["err"]), ("busy", c["busy"]), ("skipped", c["dim"])):
            self.tree.tag_configure(tag, foreground=col)
        self.tree.bind("<Double-1>", self.on_double)
        self.tree.bind("<Delete>", lambda e: self.remove_selected())
        self.tree.bind("<Button-3>", self.on_menu)
        self.menu = tk.Menu(root, tearoff=0)

        # empty-state drop zone over the list
        self.zone = tk.Canvas(mid, highlightthickness=0, bg=c["zone"], cursor="hand2")
        self.zone.bind("<Configure>", lambda e: self.draw_zone())
        self.zone.bind("<Button-1>", lambda e: self.pick_files())
        self.zone_hot = False

        if TkinterDnD:
            for w in (root, self.tree, self.zone):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self.on_drop)
                w.dnd_bind("<<DropEnter>>", lambda e: self.set_hot(True))
                w.dnd_bind("<<DropLeave>>", lambda e: self.set_hot(False))
        self.update_empty()

    def placeholder(self, entry, text):
        def show(_=None):
            if not entry.get():
                entry.insert(0, text)
                entry.configure(foreground=self.colors["dim"])
                entry.ph = True

        def hide(_=None):
            if getattr(entry, "ph", False):
                entry.delete(0, "end")
                entry.configure(foreground=self.colors["fg"])
                entry.ph = False
        entry.bind("<FocusIn>", hide)
        entry.bind("<FocusOut>", show)
        entry.get_value = lambda: "" if getattr(entry, "ph", False) else entry.get()
        show()

    def draw_zone(self):
        z, c = self.zone, self.colors
        z.delete("all")
        w, h = z.winfo_width(), z.winfo_height()
        m = self.px(2)
        col = c["accent"] if self.zone_hot else c["border"]
        z.create_rectangle(m, m, w - m, h - m, outline=col, width=self.px(2), dash=(10, 6))
        cx, cy, s = w // 2, h // 2 - self.px(34), self.px(28)
        z.create_polygon(cx, cy - s, cx + s, cy - s // 2, cx, cy, cx - s, cy - s // 2,
                         fill=c["accent"], outline="")
        z.create_polygon(cx - s, cy - s // 2, cx, cy, cx, cy + s, cx - s, cy + s // 2, fill=c["dim"], outline="")
        z.create_polygon(cx + s, cy - s // 2, cx, cy, cx, cy + s, cx + s, cy + s // 2, fill=c["border"], outline="")
        z.create_text(cx, cy + s + self.px(30), text="Drop .gltf / .glb files or folders here" if TkinterDnD
                      else "Drag and drop unavailable", fill=c["fg"], font=("Segoe UI Semibold", 15))
        z.create_text(cx, cy + s + self.px(60), text="or click to browse" + DOT + "folders are searched recursively"
                      + DOT + ".glb is converted automatically", fill=c["dim"], font=("Segoe UI", 10))

    def set_hot(self, hot):
        if self.zone_hot != hot:
            self.zone_hot = hot
            if self.zone.winfo_ismapped():
                self.draw_zone()
        return "copy"

    def update_empty(self):
        if self.items:
            self.zone.place_forget()
        else:
            self.zone.place(x=0, y=0, relwidth=1, relheight=1)

    def dark_titlebar(self, win):
        if not self.dark:
            return
        try:
            import ctypes
            win.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(ctypes.c_int(1)), 4)
        except Exception:
            pass

    # ---------------------------------------------------------------- state
    def write(self, msg, tag=None):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def load_library(self):
        try:
            p, o = library_dirs()
            self.library = existing_titles(o)
            ok = os.path.isfile(IMPORTER)
            self.lib_label.configure(text="Your output directory set by Enscape is:  " + o +
                                     ("" if ok else DOT + "Enscape batch importer not found!"))
        except Exception:
            self.library = {}
            self.lib_label.configure(text="Your output directory is not set - open the Enscape Custom Asset Editor once")

    def on_focus(self):
        if not self.busy:
            self.load_library()
            self.refresh_all()

    def final_title(self, it):
        return it["title"]

    def check_library(self, iid):
        """Mark rows whose title is already in the Output Directory (they are still imported)."""
        it = self.items[iid]
        if it["status"] in ("queued", "library"):
            it["status"] = "library" if it["title"] in self.library else "queued"
            self.refresh_row(iid)
        self.update_footer()

    def refresh_all(self):
        for iid in self.items:
            self.check_library(iid)
        self.update_footer()

    def refresh_row(self, iid):
        it = self.items[iid]
        label, tag = STATUS[it["status"]]
        self.tree.item(iid, values=(self.final_title(it), label, it["note"], it["model"]), tags=(tag,))

    def pending(self):
        return [i for i in self.tree.get_children()
                if self.items[i]["status"] in ("queued", "library", "failed", "cancelled")]

    def update_footer(self):
        if self.busy:
            return
        n, total = len(self.pending()), len(self.items)
        self.go.configure(text=GO, state="normal" if n else "disabled")
        done = sum(1 for i in self.items.values() if i["status"] == "done")
        lib = sum(1 for i in self.items.values() if i["status"] == "library")
        bits = ["%d in list" % total] if total else ["Add models to start"]
        if lib:
            bits.append("%d already present (imported again)" % lib)
        if done:
            bits.append("%d imported" % done)
        self.status.configure(text=DOT.join(bits))

    def set_thumb(self, iid, pil):
        if not self.tree.exists(iid) or pil is None:
            return
        self.photos[iid] = ImageTk.PhotoImage(self.tile(pil))
        self.tree.item(iid, image=self.photos[iid])

    def tile(self, pil=None):
        """Rounded thumbnail tile on the list background; RGBA previews keep their transparency."""
        t, big = self.thumb_px, self.thumb_px * 4
        tile = Image.new("RGB", (big, big), self.colors["bg"])
        card = Image.new("RGB", (big, big), self.colors["tile"])
        mask = Image.new("L", (big, big), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, big - 1, big - 1), big // 6, fill=255)
        if pil is not None:
            im = pil.copy()
            side = min(im.size)
            im = im.crop(((im.width - side) // 2, (im.height - side) // 2,
                          (im.width + side) // 2, (im.height + side) // 2)).resize((big, big), Image.LANCZOS)
            if im.mode == "RGBA":
                card.paste(im, (0, 0), im)
            else:
                card = im.convert("RGB")
        tile.paste(card, (0, 0), mask)
        return tile.resize((t, t), Image.LANCZOS)

    def poll(self):
        try:
            while True:
                kind, *a = self.q.get_nowait()
                if kind == "log":
                    self.write(*a)
                elif kind == "item":  # iid, fields
                    if a[0] in self.items:
                        self.items[a[0]].update(a[1])
                        self.refresh_row(a[0])
                        self.update_footer()
                elif kind == "thumb":
                    self.set_thumb(*a)
                elif kind == "add":
                    self.add_model(*a)
                elif kind == "progress":
                    if a[0] is not None:
                        self.pbar.configure(value=a[0])
                    if a[1]:
                        self.status.configure(text=a[1])
                elif kind == "footer":
                    self.update_footer()
                elif kind == "done":
                    self.busy = False
                    self.load_library()
                    self.go.configure(style="Accent.TButton")
                    self.update_footer()
                    self.status.configure(text=a[0])
        except queue.Empty:
            pass
        self.root.after(80, self.poll)

    # ---------------------------------------------------------------- adding
    def add_model(self, model, title=None, desc=""):
        model = os.path.normpath(model)
        if any(v["model"] == model for v in self.items.values()):
            return
        it = {"model": model, "title": title or default_title(model), "desc": desc, "status": "queued",
              "note": "reading..."}
        iid = self.tree.insert("", "end", text="", image=self.blank)
        self.items[iid] = it
        self.check_library(iid)
        self.update_empty()
        lib = self.library.get(self.final_title(it))

        def info():
            try:
                m = model_info(model)
                fmt = "GLB" if model.lower().endswith(".glb") else "glTF"
                note = DOT.join((fmt, fmt_size(m["size"]), "{:,} tris".format(m["tris"]),
                                 "%d mat" % m["materials"]))
                self.q.put(("item", iid, {"note": note}))
                if lib:
                    self.q.put(("thumb", iid, library_preview(lib)))
                    return
                self.q.put(("thumb", iid, m["thumb"]))
                try:
                    self.q.put(("thumb", iid, render_preview(model)))
                except Exception:
                    pass
            except Exception as e:
                self.q.put(("item", iid, {"note": "unreadable: %s" % e, "status": "failed"}))
        threading.Thread(target=info, daemon=True).start()

    def add_paths(self, paths):
        n = 0
        for p in paths:
            for m in find_models(p):
                self.add_model(m)
                n += 1
        if not n and paths:
            messagebox.showinfo(APP, "No .gltf / .glb files found in:\n" + "\n".join(paths), parent=self.root)

    def on_drop(self, event):
        self.set_hot(False)
        self.add_paths(self.root.tk.splitlist(event.data))
        return "copy"

    def pick_files(self):
        self.add_paths(filedialog.askopenfilenames(parent=self.root, filetypes=[("glTF models", "*.gltf *.glb")]))

    def pick_folder(self):
        d = filedialog.askdirectory(parent=self.root)
        if d:
            self.add_paths([d])

    # ---------------------------------------------------------------- list actions
    def remove_selected(self):
        for iid in self.tree.selection():
            if self.items[iid]["status"] == "importing":
                continue
            self.tree.delete(iid)
            self.items.pop(iid, None)
            self.photos.pop(iid, None)
        self.update_empty()
        self.update_footer()

    def clear(self):
        if self.busy:
            return
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        self.photos.clear()
        self.update_empty()
        self.update_footer()

    def on_double(self, event):
        iid = self.tree.identify_row(event.y)
        if iid and self.items[iid]["status"] != "importing":
            EditDialog(self, iid)

    def on_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        it = self.items[iid]
        m = self.menu
        m.delete(0, "end")
        m.add_command(label="Edit title / description...", command=lambda: EditDialog(self, iid),
                      state="disabled" if it["status"] == "importing" else "normal")
        m.add_command(label="Open source folder", command=lambda: os.startfile(os.path.dirname(it["model"])))
        lib = self.library.get(self.final_title(it))
        m.add_command(label="Show in Output Directory", state="normal" if lib else "disabled",
                      command=lambda: subprocess.Popen(["explorer", "/select,", os.path.dirname(lib)]))
        m.add_separator()
        m.add_command(label="Remove from list", command=self.remove_selected)
        m.tk_popup(event.x_root, event.y_root)

    # ---------------------------------------------------------------- import
    def start(self):
        if self.busy:
            self.cancel = True
            self.go.configure(text="Stopping after this one...", state="disabled")
            return
        jobs = [(iid, dict(self.items[iid])) for iid in self.pending()]
        if not jobs:
            return
        try:
            project_dir, out_dir = library_dirs()
        except Exception as e:
            messagebox.showerror(APP, "Cannot read the Enscape Custom Asset Editor settings:\n%s\n\nOpen the Enscape Custom Asset "
                                      "Editor once and set the Project and Output directories." % e, parent=self.root)
            return
        if not os.path.isfile(IMPORTER):
            messagebox.showerror(APP, "Enscape batch importer not found:\n%s\n\nIs Enscape installed?" % IMPORTER,
                                 parent=self.root)
            return
        self.busy, self.cancel = True, False
        self.go.configure(text="Stop", style="TButton")
        self.pbar.configure(maximum=len(jobs), value=0)

        def work():
            import time
            ok = fail = 0
            for n, (iid, j) in enumerate(jobs):
                if self.cancel:
                    for jid, _ in jobs[n:]:
                        self.q.put(("item", jid, {"status": "cancelled"}))
                    break
                title = j["title"]
                self.q.put(("progress", n, "Importing %d of %d: %s" % (n + 1, len(jobs), title)))
                self.q.put(("item", iid, {"status": "importing"}))
                self.q.put(("log", "Importing %s ..." % title))
                t0 = time.time()
                try:
                    guid, report = import_one(j["model"], title, j["desc"], project_dir, out_dir)
                    metal = [r.rsplit(" ", 1)[-1] for r in report if re.search(r" \d+\.\d\d$", r)]
                    note = DOT.join((j["note"].split(DOT)[0], "metallic " + (", ".join(metal) or "-"),
                                     "%ds" % (time.time() - t0)))
                    self.q.put(("item", iid, {"status": "done", "note": note}))
                    self.q.put(("log", "  ok  %s  [%s]" % (guid, "; ".join(report)), "ok"))
                    lj = glob.glob(os.path.join(out_dir, "*_" + guid, guid + ".json"))
                    if lj:
                        self.q.put(("thumb", iid, library_preview(lj[0])))
                    ok += 1
                except Exception as e:
                    self.q.put(("item", iid, {"status": "failed", "note": str(e).splitlines()[0][:90]}))
                    self.q.put(("log", "  FAILED: %s" % e, "err"))
                    fail += 1
                self.q.put(("progress", n + 1, ""))
            summary = "Finished: %d imported" % ok + (", %d failed (see log)" % fail if fail else "") + \
                      (" - stopped" if self.cancel else "")
            self.q.put(("progress", len(jobs), ""))
            self.q.put(("log", summary, "err" if fail else "ok"))
            self.q.put(("done", summary))
        threading.Thread(target=work, daemon=True).start()


def app_dir():
    return os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))


def resource(name):
    return os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), name)


def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on high-DPI screens
    except Exception:
        pass
    root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
    app = App(root)
    paths = [p for p in sys.argv[1:] if os.path.exists(p)]  # files dropped onto the exe icon
    if paths:
        app.add_paths(paths)
    root.mainloop()


if __name__ == "__main__":
    main()
