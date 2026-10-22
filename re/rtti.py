import sys, struct, re, pefile
pe = pefile.PE(sys.argv[1]); base = pe.OPTIONAL_HEADER.ImageBase
img = pe.get_memory_mapped_image()
def u32(va): return struct.unpack_from('<I', img, va-base)[0]
def inimg(v): return base <= v < base+len(img)
# type descriptors
tds = {}
for m in re.finditer(rb'\.\?A[VU][^\x00]+@@\x00', img):
    td = base + m.start() - 8
    tds[td] = m.group()[:-1].decode()
# COLs
cols = {}
for off in range(0, len(img)-20, 4):
    sig, o, cd, ptd, pch = struct.unpack_from('<5I', img, off)
    if sig == 0 and ptd in tds and inimg(pch):
        cols[base+off] = (tds[ptd], o)
# vtables
text = [s for s in pe.sections if s.Name.startswith(b'.text')][0]
tlo, thi = base+text.VirtualAddress, base+text.VirtualAddress+text.Misc_VirtualSize
for off in range(4, len(img)-4, 4):
    v = struct.unpack_from('<I', img, off-4)[0]
    if v in cols:
        name, o = cols[v]; vt = base+off
        fns = []
        i = 0
        while True:
            f = u32(vt+4*i)
            if not (tlo <= f < thi): break
            if i > 0 and (vt+4*i - base - 4) >= 0 and struct.unpack_from('<I', img, vt+4*i-base-4)[0] in cols and i>0: break
            fns.append(f); i += 1
            if i > 200: break
        print(f"VTABLE {vt:08x} {name} off={o} n={len(fns)}")
        for i,f in enumerate(fns): print(f"  [{i}] {f:08x}")
