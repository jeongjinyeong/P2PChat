from socket import *
import json
import asyncio
import threading
import sys

lock = threading.Lock()

sBuffer = []
MSS = 16
RTT = 3
timer = None

cwnd = 1
sendBase = 0
sidx = 0
seqNum = 0 # assign random number when sock is connected but not yet implemented
cack = seqNum
offset = 3
reserved = 0
flags = 0
window = 5 # window is temp value. it should be replaced by cwnd but not yet implemented

class Timer:
  def __init__(self, timeout, sock, address, callback):
    self._timeout = timeout
    self._sock = sock
    self._address = address
    self._callback = callback
    self._task = asyncio.ensure_future(self.task())

  async def task(self):
    await asyncio.sleep(self._timeout)
    await self._callback(self._sock, self._address)

  def cancel(self):
    self._task.cancel()

async def timeout_callback(sock, address):
  global sendBase, ssthresh, cwnd, dupACKcount, sBuffer, sidx, timer
  await asyncio.sleep(0.1)

  lock.acquire()

  ssthresh = cwnd/2
  cwnd = 1
  dupACKcount=0

  sidx = sendBase
  sock.sendto(sBuffer[sidx], address)
  sidx+=1

  timer = Timer(RTT, sock, address, timeout_callback)

  lock.release()

async def ainput(prompt: str=''):
  return await asyncio.to_thread(input, prompt)

'''
header = b''
Sequence Number(32bits)
Acknowledgement Number(32bits)
Offset(4bits)|Reserved(4bits)|Flags(C,E,U,A,P,R,S,F)|Window(16bits)
'''

async def send(sock, targetName, targetPort):
  global sidx, seqNum, timer
  while True:
    message = await ainput('')
    message += '\0'
    
    while message!='':
      header = seqNum.to_bytes(4,'big')+cack.to_bytes(4,'big')+(offset<<4|reserved).to_bytes(1,'big')+flags.to_bytes(1,'big')+window.to_bytes(2,'big')
      data = header + str.encode(message[:MSS-offset*4])
      lock.acquire()
      seqNum+=len(data)
      lock.release()
      sBuffer.append(data)
      message = message[MSS-offset*4:]

    # start timer if no timer exists in run state
    if sendBase==sidx:
      timer = Timer(RTT, sock, (targetName, targetPort), timeout_callback)

    # send packet as possible
    while sidx<sendBase+cwnd and sidx<len(sBuffer):
      sock.sendto(sBuffer[sidx], (targetName, targetPort))
      if (sBuffer[sidx][8]>>4)<<2==len(sBuffer[sidx]):
        sBuffer.remove(sBuffer[sidx])
        continue
      lock.acquire()
      sidx+=1
      lock.release()

async def recv(sock):
  global seqNum, cack, offset, reserved, flags, cwnd, window, sidx, sendBase, timer

  rBuffer = []
  ridx = 0
  message = ''

  lastACK = -1
  dupACKcount = 0
  ACKcount = 0
  ssthresh = 100

  while True:
    (recvString, address) = await loop.run_in_executor(None, sock.recvfrom, 2048)
    
    seq = recvString[0:4]
    ack = recvString[4:8]
    _offset = recvString[8]>>4
    _offset = _offset<<2 # Word to Byte
    _flags = recvString[9]

    if seq>=cack.to_bytes(4,'big'):
      rBuffer.append(recvString)
      rBuffer.sort()
    for i in range(ridx, len(rBuffer)):
      if rBuffer[i][0:4]==cack.to_bytes(4,'big'): # if cack equals with rBuffer pkt's seq then cack increase about pkt length
        ridx+=1
        cack+=len(rBuffer[i])
        ioffset = rBuffer[i][8]>>4
        ioffset = ioffset<<2
        message+=rBuffer[i][ioffset:].decode()
        if len(message)!=0 and message[-1]=='\0':
          print('>>'+message)
          message = ''
      else:
        break

    # send cack using sBuffer
    if _offset!=len(recvString): # except no message and header only packet, because they do not need to ACK back
      i = sidx
      for i in range(sidx,len(sBuffer)):
        if not sBuffer[i][9]&16: # send ACK using 'not yet sent packet' in sBuffer
          pkt = list(sBuffer[i])
          pkt[9] = pkt[9]|16
          pkt[4:8] = list(cack.to_bytes(4,'big'))
          pkt = [x.to_bytes(1,'big') for x in pkt]
          sBuffer[i] = b''.join(pkt)
          break
      if i==len(sBuffer): # if every 'not yet sent packet' have ACK, then create new packet for ACK and append to sBuffer
        data = seqNum.to_bytes(4,'big')+cack.to_bytes(4,'big')+(offset<<4|reserved).to_bytes(1,'big')+(flags|16).to_bytes(1,'big')+window.to_bytes(2,'big')
        seqNum+=len(data)
        sBuffer.append(data)
    
    # check ACK flag
    if _flags&16:
      # timer cancel when receive ACK
      timer.cancel()
      # duplicate ACK
      if int.from_bytes(ack,'big')==lastACK:
        dupACKcount+=1
        if dupACKcount==3:
          ssthresh = cwnd/2
          lock.acquire()
          cwnd = ssthresh+3
          lock.release()
          # retransmit missing segment
          for i in range(sendBase, sidx):
            if sBuffer[i][0:4]==ack:
              sock.sendto(sBuffer[i],address)
        # Fast Recovery - duplicate ACK
        elif dupACKcount>3:
          lock.acquire()
          cwnd+=1
          lock.release()
      
      # new ACK
      elif int.from_bytes(ack,'big')!=lastACK:
        # Fast Recovery - new ACK
        if dupACKcount>=3:
          lock.acquire()
          cwnd=ssthresh
          lock.release()
          dupACKcount=0

        lastACK = int.from_bytes(ack,'big')
        if cwnd<ssthresh:
          # increase 1cwnd per 1ACK
          lock.acquire()
          cwnd+=1
          lock.release()
        else:
          # increase 1cwnd per 1RTT
          ACKcount+=1
          if ACKcount==cwnd:
            lock.acquire()
            cwnd+=1
            lock.release()
            ACKcount=0
        dupACKcount=0
        while sendBase<len(sBuffer) and int.from_bytes(sBuffer[sendBase][0:4],'big')<int.from_bytes(ack,'big'):
          lock.acquire()
          sendBase+=1
          lock.release()
      # if their exist not yet ACKed packet then timer restart
      if sendBase!=sidx:
        timer = Timer(RTT, sock, address, timeout_callback)

    while sidx<sendBase+cwnd and sidx<len(sBuffer):
      sock.sendto(sBuffer[sidx],address)
      # if sBuffer[sidx] is just ACK packet then remove it after sending packet
      if(sBuffer[sidx][8]>>4)<<2==len(sBuffer[sidx]):
        sBuffer.remove(sBuffer[sidx])
        continue
      lock.acquire()
      sidx+=1
      lock.release()

async def main():
  # it is for test
  # In real condition, userPort is my Port and (targetName, targetPort) is target's IP and Port number.
  userPort = input('userPort: ')
  targetPort = input('targetPort: ')
  userPort = int(userPort)
  targetPort = int(targetPort)
  targetName = '127.0.0.1'

  userSocket = socket(AF_INET, SOCK_DGRAM)
  userSocket.bind(('', userPort))
  
  task1 = asyncio.create_task(send(userSocket, targetName, targetPort))
  task2 = asyncio.create_task(recv(userSocket))

  await task1
  await task2

  userSocket.close()

loop = asyncio.get_event_loop()
loop.run_until_complete(main())
loop.close()
