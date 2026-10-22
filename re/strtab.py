import sys, struct, pefile
pe = pefile.PE(sys.argv[1])
for rt in pe.DIRECTORY_ENTRY_RESOURCE.entries:
    if rt.id != 6: continue  # RT_STRING
    for blk in rt.directory.entries:
        for lang in blk.directory.entries:
            d = lang.data.struct
            data = pe.get_data(d.OffsetToData, d.Size)
            off = 0
            for i in range(16):
                n = struct.unpack_from('<H', data, off)[0]; off += 2
                s = data[off:off+2*n].decode('utf-16le'); off += 2*n
                if s: print(f"{(blk.id-1)*16+i:#06x}\t{s!r}")
