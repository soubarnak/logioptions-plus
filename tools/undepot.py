import sys,json,struct,os
def unpack(path,out):
    d=open(path,'rb').read()
    magic=d[:4]; n=struct.unpack_from('<I',d,4)[0]
    j=json.loads(d[8:8+n]); p=8+n
    os.makedirs(out,exist_ok=True)
    for f in j['files']:
        sz=struct.unpack_from('<I',d,p)[0]; p+=4
        name=f['name'].replace('\\','/')
        dst=os.path.normpath(os.path.join(out,name))
        assert dst.startswith(os.path.abspath(out)) or dst.startswith(out), name
        os.makedirs(os.path.dirname(dst) or out,exist_ok=True)
        if name.endswith('/'): continue
        open(dst,'wb').write(d[p:p+sz]); p+=sz
    return magic.hex(), len(j['files']), p==len(d)
if __name__=='__main__':
    print(unpack(sys.argv[1],sys.argv[2]))
