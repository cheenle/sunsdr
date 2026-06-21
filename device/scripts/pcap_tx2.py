import sys, struct
import numpy as np

def packets(path):
    with open(path,'rb') as f:
        data=f.read()
    # global header 24 bytes
    magic=struct.unpack('<I',data[:4])[0]
    le = magic in (0xa1b2c3d4,0xa1b23c4d)
    endian='<' if le else '>'
    nano = magic in (0xa1b23c4d,0x4d3cb2a1)
    off=24
    n=len(data)
    while off+16<=n:
        ts_s,ts_u,incl,orig=struct.unpack(endian+'IIII',data[off:off+16])
        off+=16
        pkt=data[off:off+incl]; off+=incl
        yield ts_s+ts_u/(1e9 if nano else 1e6), pkt

def parse_udp(pkt):
    if len(pkt)<14: return None
    eth=pkt[12:14]
    if eth!=b'\x08\x00':
        # maybe loopback / null
        return None
    ihl=(pkt[14]&0x0f)*4
    if pkt[23]!=17: return None
    ipoff=14
    src='.'.join(str(b) for b in pkt[ipoff+12:ipoff+16])
    dst='.'.join(str(b) for b in pkt[ipoff+16:ipoff+20])
    udp=ipoff+ihl
    sport,dport,ulen,_=struct.unpack('>HHHH',pkt[udp:udp+8])
    payload=pkt[udp+8:]
    return src,dst,sport,dport,payload

path=sys.argv[1]
DEV='192.168.16.200'
fffd=[]  # (ts, counter, payload)
ctrl=[]  # (ts, cmd, trailing, payload) to dev:50001
for ts,pkt in packets(path):
    u=parse_udp(pkt)
    if not u: continue
    src,dst,sp,dp,pl=u
    if dst==DEV and dp==50002 and len(pl)>=10:
        sub=struct.unpack('<H',pl[2:4])[0]
        cnt=struct.unpack('<I',pl[4:8])[0]
        flags=struct.unpack('<H',pl[8:10])[0]
        if sub==0xfffd:
            fffd.append((ts,cnt,flags,pl[10:]))
    elif dst==DEV and dp==50001 and len(pl)>=14:
        cmd=struct.unpack('<H',pl[2:4])[0]
        dlen=struct.unpack('<I',pl[4:8])[0]
        trail=pl[14+dlen:14+dlen+4]
        ctrl.append((ts,cmd,pl[14:14+dlen],trail))

print("=== 0xFFFD TX IQ packets:",len(fffd))
if fffd:
    # counter pattern
    cnts=[c for _,c,_,_ in fffd[:8]]
    print("first counters:",[hex(c) for c in cnts])
    diffs=[fffd[i+1][1]-fffd[i][1] for i in range(min(8,len(fffd)-1))]
    print("counter diffs:",[hex(d) for d in diffs])
    # timing
    ts0=[t for t,_,_,_ in fffd]
    dts=np.diff(ts0)
    print(f"interval mean={dts.mean()*1000:.3f}ms median={np.median(dts)*1000:.3f}ms min={dts.min()*1000:.3f} max={dts.max()*1000:.3f}")
    # decode IQ of a mid packet
    def decode(payload):
        n=min(200,len(payload)//6)
        iq=np.zeros(n,dtype=np.complex64)
        for i in range(n):
            o=i*6
            iv=int.from_bytes(payload[o:o+3],'little',signed=True)
            qv=int.from_bytes(payload[o+3:o+6],'little',signed=True)
            iq[i]=complex(iv/8388608.0,qv/8388608.0)
        return iq
    # find a packet with energy
    mids=fffd[len(fffd)//2:len(fffd)//2+30]
    for idx,(t,c,fl,p) in enumerate(mids):
        iq=decode(p)
        amp=np.abs(iq)
        if amp.max()>1e-4:
            print(f"\npkt#{len(fffd)//2+idx} flags={hex(fl)} len_payload={len(p)} nsamp={len(iq)}")
            print(f"  |IQ| max={amp.max():.5f} mean={amp.mean():.5f} rms={np.sqrt((amp**2).mean()):.5f}")
            # estimate tone freq via phase diff
            ph=np.angle(iq[1:]*np.conj(iq[:-1]))
            f=np.mean(ph)/(2*np.pi)*78125
            print(f"  est tone freq={f:.1f} Hz  (mean dphase)")
            break
    # overall energy distribution
    energetic=0
    maxamp=0
    for t,c,fl,p in fffd:
        iq=decode(p)
        m=np.abs(iq).max()
        if m>1e-4: energetic+=1
        maxamp=max(maxamp,m)
    print(f"\nenergetic packets={energetic}/{len(fffd)}  global max|IQ|={maxamp:.5f}")

print("\n=== Ctrl pkts to dev:50001 during capture:",len(ctrl))
from collections import Counter
cc=Counter((hex(cmd)) for _,cmd,_,_ in ctrl)
print("cmd histogram:",dict(cc))
# show PTT (0x0006) packets with trailing
print("\n-- PTT (0x0006) events --")
for t,cmd,pl,trail in ctrl:
    if cmd==0x0006:
        tv=struct.unpack('<I',trail)[0] if len(trail)>=4 else None
        print(f"  t={t:.3f} payload={pl.hex()} trailing={hex(tv) if tv is not None else trail.hex()}")
