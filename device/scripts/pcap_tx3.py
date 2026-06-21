import sys, struct, numpy as np

def packets(path):
    with open(path,'rb') as f:
        d=f.read()
    magic=struct.unpack('<I',d[:4])[0]
    le = magic in (0xa1b2c3d4,0xa1b23c4d)
    nano = magic in (0xa1b23c4d,0x4d3cb2a1)
    end='<' if le else '>'
    off=24
    while off+16<=len(d):
        ts_s,ts_f,incl,orig=struct.unpack(end+'IIII',d[off:off+16])
        off+=16
        pkt=d[off:off+incl]; off+=incl
        ts=ts_s+ts_f/(1e9 if nano else 1e6)
        yield ts,pkt

def udp(pkt):
    if len(pkt)<14: return None
    et=struct.unpack('>H',pkt[12:14])[0]
    l3=14
    if et==0x8100: l3=18; et=struct.unpack('>H',pkt[16:18])[0]
    if et!=0x0800: return None
    ih=pkt[l3]; ihl=(ih&0xf)*4
    if pkt[l3+9]!=17: return None
    src='.'.join(map(str,pkt[l3+12:l3+16]))
    dst='.'.join(map(str,pkt[l3+16:l3+20]))
    u=l3+ihl
    sp,dp,ul=struct.unpack('>HHH',pkt[u:u+6])
    return src,dst,sp,dp,pkt[u+8:]

DEV='192.168.16.200'
rows=[]
for ts,pkt in packets(sys.argv[1]):
    r=udp(pkt)
    if not r: continue
    src,dst,sp,dp,pl=r
    if dst==DEV and dp==50002 and len(pl)>=10:
        sub=struct.unpack('<H',pl[2:4])[0]
        rows.append((ts,sub,pl))

# Find PTT-active TX bursts: look at 0xFFFD runs
t0=rows[0][0]
print("First/last 0xFFFD vs 0xFFFE timeline (relative s):")
# Identify contiguous 0xFFFD bursts
bursts=[]; cur=None
for ts,sub,pl in rows:
    if sub==0xFFFD:
        if cur is None: cur=[ts,ts,0]
        cur[1]=ts; cur[2]+=1
    else:
        if cur is not None: bursts.append(cur); cur=None
if cur: bursts.append(cur)
print(f"\n0xFFFD bursts: {len(bursts)}")
for b in bursts[:8]:
    print(f"  start={b[0]-t0:7.3f}s dur={b[1]-b[0]:6.3f}s pkts={b[2]} rate={b[2]/max(b[1]-b[0],1e-6):.1f}/s")

# Envelope of one energetic burst: decode 24-bit IQ, measure per-packet RMS
def decode(pl):
    p=pl[10:]; n=min(200,len(p)//6)
    iq=np.zeros(n,np.complex64)
    for i in range(n):
        o=i*6
        iv=int.from_bytes(p[o:o+3],'little',signed=True)
        qv=int.from_bytes(p[o+3:o+6],'little',signed=True)
        iq[i]=complex(iv/8388608.0,qv/8388608.0)
    return iq

# pick the burst with most packets
big=max(bursts,key=lambda b:b[2])
print(f"\nAnalyzing biggest burst start={big[0]-t0:.3f} pkts={big[2]}")
seq=[(ts,pl) for ts,sub,pl in rows if sub==0xFFFD and big[0]<=ts<=big[1]]
rms=[]; peak=[]
for ts,pl in seq:
    iq=decode(pl)
    rms.append(float(np.sqrt(np.mean(np.abs(iq)**2))))
    peak.append(float(np.max(np.abs(iq))) if len(iq) else 0)
rms=np.array(rms); peak=np.array(peak)
print(f"  per-pkt RMS: min={rms.min():.5f} max={rms.max():.5f} mean={rms.mean():.5f}")
print(f"  per-pkt peak: min={peak.min():.5f} max={peak.max():.5f}")
nz=np.where(rms>1e-4)[0]
print(f"  first non-silent pkt idx={nz[0] if len(nz) else -1}/{len(rms)} (ramp/leading silence)")
print(f"  RMS envelope (every 10th pkt): {np.round(rms[::10],4).tolist()}")

# Look at the header bytes of an energetic 0xFFFD packet
en=[pl for ts,pl in seq if np.sqrt(np.mean(np.abs(decode(pl))**2))>0.01]
if en:
    h=en[0][:16]
    print(f"\n0xFFFD header (16B) of energetic pkt: {h.hex()}")
    sub,=struct.unpack('<H',en[0][2:4])
    cnt,=struct.unpack('<I',en[0][4:8])
    flags,=struct.unpack('<H',en[0][8:10])
    print(f"  magic={en[0][:2].hex()} sub={sub:#06x} counter={cnt:#010x} flags={flags:#06x}")
