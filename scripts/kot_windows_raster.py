"""Small GDI drawing surface for Unicode KOT raster output on Windows."""
import ctypes as c
from ctypes import wintypes as w

class DrawingSurface:
    def __init__(self):
        self.gdi=c.WinDLL('gdi32',use_last_error=True)
        self.user=c.WinDLL('user32',use_last_error=True)
        signatures = {
            'CreateCompatibleDC':([w.HDC],w.HDC),
            'SelectObject':([w.HDC,w.HANDLE],w.HANDLE),
            'DeleteObject':([w.HANDLE],w.BOOL),
            'DeleteDC':([w.HDC],w.BOOL),
            'CreateSolidBrush':([w.DWORD],w.HANDLE),
            'SetTextColor':([w.HDC,w.DWORD],w.DWORD),
            'SetBkMode':([w.HDC,c.c_int],c.c_int),
            'CreateDIBSection':([w.HDC,c.c_void_p,w.UINT,c.POINTER(c.c_void_p),w.HANDLE,w.DWORD],w.HANDLE),
            'CreateFontW':([c.c_int]*5+[w.DWORD]*8+[w.LPCWSTR],w.HANDLE),
        }
        for name,(args,result) in signatures.items():
            fn=getattr(self.gdi,name);fn.argtypes=args;fn.restype=result
        self.user.FillRect.argtypes=[w.HDC,c.POINTER(w.RECT),w.HANDLE]
        self.user.FillRect.restype=c.c_int
        self.dc=self.gdi.CreateCompatibleDC(None)
        if not self.dc:raise c.WinError(c.get_last_error())
        self.objects=[]
    def font(self,family):
        handle=self.gdi.CreateFontW(-28,0,0,0,500,0,0,0,1,0,0,4,0,family)
        if not handle:raise c.WinError(c.get_last_error())
        self.objects.append(handle);return handle
    def SelectObject(self,obj):self.gdi.SelectObject(self.dc,obj)
    def GetSafeHdc(self):return self.dc
    def SetTextColor(self,value):self.gdi.SetTextColor(self.dc,value)
    def SetBkMode(self,value):self.gdi.SetBkMode(self.dc,value)
    def FillSolidRect(self,rect,color):
        x,y,width,height=map(int,rect)
        brush=self.gdi.CreateSolidBrush(color)
        try:self.user.FillRect(self.dc,c.byref(w.RECT(x,y,x+width,y+height)),brush)
        finally:self.gdi.DeleteObject(brush)
    def bitmap(self,width,height):
        import struct
        header=c.create_string_buffer(struct.pack('<IiiHHIIiiII',40,width,-height,1,32,0,width*height*4,0,0,0,0))
        self.bits=c.c_void_p();self.size=width*height*4
        handle=self.gdi.CreateDIBSection(self.dc,header,0,c.byref(self.bits),None,0)
        if not handle:raise c.WinError(c.get_last_error())
        self.objects.append(handle);self.SelectObject(handle)
    def pixels(self):
        self.gdi.GdiFlush()
        return c.string_at(self.bits,self.size)
    def close(self):
        self.gdi.DeleteDC(self.dc)
        for obj in self.objects:self.gdi.DeleteObject(obj)
