import sys, struct, statistics
import numpy as np

def packets(path):
    with open(path,'rb') as f:
        d=f.read()
    magic=struct.unpack('<I',d[:4])[0]
    le = magic in (0xa1b2c3d4,0xa1b23c4d)
    nano = magic in (0xa1b23c4d,0x4d3cb2a1)
    end='<' if le else '>'
    off=24
    while off+16<=len(d):
        ts_s,ts_f,incl,orig=struct.unpack(end+'IIII',d[off:off+16]); off+=16
        if off+incl>len(d): break
        yield ts_s+ts_f/(1e9 if nano else 1e6), d[off:off+incl]; off+=incl

def udp_payload(pkt):
    if len(pkt)<14: return None
    eth=pkt[12:14]
    if eth!=b'\x08\x00':
        if pkt[12:14]==b'\x81\x00': ipoff=18
        else: return None
    else: ipoff=14
    if len(pkt)<ipoff+20: return None
    if (pkt[ipoff]>>4)!=4: return None
    ihl=(pkt[ipoff]&0xf)*4
    proto=pkt[ipoff+9]
    if proto!=17: return None
    src='.'.join(str(b) for b in pkt[ipoff+12:ipoff+16])
    dst='.'.join(str(b) for b in pkt[ipoff+16:ipoff+20])
    uoff=ipoff+ihl
    if len(pkt)<uoff+8: return None
    sport,dport,ulen,_=struct.unpack('>HHHH',pkt[uoff:uoff+8])
    return src,dst,sport,dport,pkt[uoff+8:]

PATH=sys.argv[1]
# collect 0xFFFD timestamps
fdd=[]
for ts,pkt in packets(PATH):
    r=udp_payload(pkt)
    if not r: continue
    src,dst,sp,dp,pl=r
    if dp==50002 and len(pl)>=4 and pl[0]==0x32 and pl[1]==0xff:
        sub=struct.unpack('<H',pl[2:4])[0]
        if sub==0xFFFD: fdd.append(ts)

fdd.sort()
# split into bursts (gap>0.1s)
bursts=[]; cur=[fdd[0]]
for t in fdd[1:]:
    if t-cur[-1]>0.1: bursts.append(cur); cur=[t]
    else: cur.append(t)
bursts.append(cur)

print(f"total 0xFFFD={len(fdd)} bursts={len(bursts)}")
for b in bursts:
    if len(b)<5: continue
    iv=np.diff(b)*1000
    dur=b[-1]-b[0]
    nsamp=len(b)*200
    sr=nsamp/dur if dur>0 else 0
    print(f"  burst pkts={len(b)} dur={dur:.3f}s rate={len(b)/dur:.1f}pkt/s "
          f"iv(ms) med={np.median(iv):.3f} mean={iv.mean():.3f} p10={np.percentile(iv,10):.3f} p90={np.percentile(iv,90):.3f} "
          f"-> impliedTXsr={sr:.0f}Hz")
