import sys,json,struct,os
p,out=sys.argv[1],sys.argv[2]
f=open(p,'rb'); h=f.read(16)
size=struct.unpack_from('<I',h,12)[0]
hdr=json.loads(f.read(size)); base=16+size
# header pickle: [4 payload size][4 hdr pickle size][4 str pickle][4 strlen]; base aligned
base=8+struct.unpack_from('<I',h,4)[0]
def walk(node,path):
    for k,v in node.get('files',{}).items():
        dst=os.path.join(path,k)
        if 'files' in v: os.makedirs(dst,exist_ok=True); walk(v,dst)
        elif v.get('unpacked'): pass
        elif 'link' in v: pass
        else:
            f.seek(base+int(v['offset'])); open(dst,'wb').write(f.read(v['size']))
os.makedirs(out,exist_ok=True); walk(hdr,out)
