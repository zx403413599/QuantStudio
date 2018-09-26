# coding=utf-8
import uuid
import mmap
import pickle
import gc
from multiprocessing import Process, Queue

import numpy as np
import pandas as pd
from traits.api import Int

from QuantStudio import __QS_Object__
from .RiskModelFun import dropRiskMatrixNA

# 风险数据源基类
class RiskDataSource(__QS_Object__):
    def __init__(self, name, risk_db, table_name, sys_args={}, config_file=None, **kwargs):
        self._Name = name
        self._RiskDB = risk_db# 风险数据库
        self._TableName = table_name# 风险数据所在的表
        self._DTs = []# 数据源可提取的最长时点序列，['20090101']
        return super().__init__(sys_args=sys_args, config_file=config_file, **kwargs)
    @property
    def Name(self):
        return self._Name
    # 设置时间点序列
    def setDateTime(self, dts):
        self._DTs = sorted(dts)
    # 启动遍历模式, dts: 遍历的时间点序列或者迭代器
    def start(self, dts, **kwargs):
        return 0
    # 时点向前移动, idt:当前时点
    def move(self, idt, *args, **kwargs):
        return 0
    # 结束遍历模式
    def end(self):
        return 0
    # 获取 ID, idt: 某个时点, 返回对应该时点的个股风险不缺失的ID序列
    def getID(self, idt):
        iCov = self.readCov(idt, drop_na=True)
        return iCov.index.tolist()
    # 获取时间点序列
    def getDateTime(self):
        return self._DTs
    # 给定单个时点, 提取个股的协方差矩阵
    def readCov(self, idt, ids=None, drop_na=True):
        return self._RiskDB.readCov(self._TableName, dates=idt, ids=ids, drop_na=drop_na)

# 基于mmap的带并行局部缓冲的风险数据源, 如果开启遍历模式, 那么限制缓冲的时点长度, 缓冲区里是部分时点序列数据, 如果未开启, 则调用 RiskDataSource 提取数据的方法. 适合遍历数据, 内存消耗小, 首次提取时间不长
def _prepareRDSMMAPCacheData(arg):
    CacheData = {}
    CacheDTs = []
    while True:
        Task = arg["Queue2SubProcess"].get()
        if Task is None:
            break
        if (Task[0] is None) and (Task[1] is None):# 把数据装入缓冲区
            CacheDataByte = pickle.dumps(CacheData)
            DataLen = len(CacheDataByte)
            Msg = None
            if arg["TagName"] is not None:
                MMAPCacheData = mmap.mmap(-1, DataLen, tagname=arg["TagName"])# 当前MMAP缓存区
            else:
                MMAPCacheData = mmap.mmap(-1, DataLen)
                Msg = MMAPCacheData
            MMAPCacheData.seek(0)
            MMAPCacheData.write(CacheDataByte)
            CacheDataByte = None
            arg["Queue2MainProcess"].put((DataLen,Msg))
            Msg = None
            gc.collect()
        else:# 准备缓冲区
            MMAPCacheData = None
            CurInd = Task[0]+arg['ForwardPeriod']+1
            if CurInd<arg["DTNum"]:# 未到结尾处, 需要再准备缓存数据
                OldCacheDTs = CacheDTs
                CacheDTs = arg["DTs"][max(0, CurInd-arg['BackwardPeriod']):min(arg["DTNum"], CurInd+arg['ForwardPeriod']+1)]
                NewCacheDTs = list(set(CacheDTs).difference(set(OldCacheDTs)))
                NewCacheDTs.sort()
                DropDTs = set(OldCacheDTs).difference(set(CacheDTs))
                for iDT in DropDTs: CacheData.pop(iDT)
                Cov = arg["RiskDB"].readCov(arg['TableName'], dts=NewCacheDTs)
                for iDT in NewCacheDTs:
                    CacheData[iDT] = {}
                    CacheData[iDT]["Cov"] = Cov[iDT]
    return 0

class ParaMMAPCacheRDS(RiskDataSource):
    ForwardPeriod = Int(12, arg_type="Integer", label="向前缓冲时点数", order=0)
    BackwardPeriod = Int(0, arg_type="Integer", label="向后缓冲时点数", order=1)
    def __init__(self, name, risk_db, table_name, sys_args={}, config_file=None, **kwargs):
        super().__init__(self, name=name, risk_db=risk_db, table_name=table_name, sys_args=sys_args, config_file=config_file, **kwargs)
        # 遍历模式变量
        self.CurInd = -1# 当前时点在self._DTs中的位置, 以此作为缓冲数据的依据
        self.DTNum = None# 时点数
        self.CacheDTs = []# 缓冲的时点序列
        self.CacheData = {}# 当前缓冲区,{"Cov":DataFrame(因子协方差,index=[因子],columns=[因子])}
        self.Queue2SubProcess = None# 主进程向数据准备子进程发送消息的管道
        self.Queue2MainProcess = None# 数据准备子进程向主进程发送消息的管道
        self.CacheFun = _prepareRDSMMAPCacheData
        return
    def readCov(self, idt, ids=None, drop_na=True):
        CovMatrix = self.CacheData.get(idt)
        if CovMatrix is None:# 非遍历模式或者缓冲区无数据
            CovMatrix = self.RiskDB.readCov(self._TableName, dts=idt)
        else:
            CovMatrix = CovMatrix.get("Cov")
        if CovMatrix is None:
            return None
        if ids is not None:
            CovMatrix = CovMatrix.ix[ids, ids]
        if drop_na:
            return dropRiskMatrixNA(CovMatrix)
        return CovMatrix
    def start(self, dts, **kwargs):
        self.CurInd = -1
        self.DTNum = len(self._DTs)
        self.CacheDTs = []
        self.CacheData = {}
        self.CacheFactorNum = 0
        self.Queue2SubProcess = Queue()
        self.Queue2MainProcess = Queue()
        arg = {}
        arg['Queue2SubProcess'] = self.Queue2SubProcess
        arg['Queue2MainProcess'] = self.Queue2MainProcess
        arg['DSName'] = self.Name
        arg['RiskDB'] = self._RiskDB
        arg["TableName"] = self._TableName
        arg["DTs"] = self._DTs
        arg['DTNum'] = self.DTNum
        arg['ForwardPeriod'] = self.ForwardPeriod
        arg['BackwardPeriod'] = self.BackwardPeriod
        arg["TagName"] = self.TagName = (str(uuid.uuid1()) if os.name=="nt" else None)# 共享内存的 tag
        self.CacheDataProcess = Process(target=self.CacheFun,args=(arg,),daemon=True)
        self.CacheDataProcess.start()
        self.TempDTs = pd.Series(self._DTs)
        return 0
    def move(self, idt, *args, **kwargs):
        PreInd = self.CurInd
        self.CurInd = (self.TempDTs<=idt).sum()-1
        if (self.CurInd>-1) and ((self.CacheDTs==[]) or (self._DTs[self.CurInd]>self.CacheDTs[-1])):# 需要读入缓冲区的数据
            self.Queue2SubProcess.put((None,None))
            DataLen, Msg = self.Queue2MainProcess.get()
            if self.TagName is not None:
                MMAPCacheData = mmap.mmap(-1, DataLen, tagname=self.TagName)# 当前共享内存缓冲区
            else:
                MMAPCacheData = Msg
                Msg = None
            if self.CurInd==PreInd+1:# 没有跳跃, 连续型遍历
                self.Queue2SubProcess.put((self.CurInd,None))
                self.CacheDTs = self._DTs[max(0, self.CurInd-self.BackwardPeriod):min(self.DTNum, self.CurInd+self.ForwardPeriod+1)]
            else:# 出现了跳跃
                LastCacheInd = (self._DTs.index(self.CacheDTs[-1]) if self.CacheDTs!=[] else self.CurInd-1)
                self.Queue2SubProcess.put((LastCacheInd+1, None))
                self.CacheDTs = self._DTs[max(0, LastCacheInd+1-self.BackwardPeriod):min(self.DTNum, LastCacheInd+1+self.ForwardPeriod+1)]
            MMAPCacheData.seek(0)
            self.CacheData = pickle.loads(MMAPCacheData.read(DataLen))
        return 0
    def end(self):
        self.CacheData = {}
        self.Queue2SubProcess.put(None)
        return 0

# 多因子风险数据源基类,主要元素如下:
# 因子风险矩阵: FactorCov, DataFrame(data=协方差,index=因子,columns=因子)
# 特异性风险: SpecificRisk, Series(data=方差,index=ID)
# 因子截面数据: FactorData, DataFrame(data=因子数据,index=ID,columns=因子)
# 因子收益率: FactorReturn, Series(data=收益率,index=因子)
# 特异性收益率: SpecificReturn, Series(data=收益率,index=ID)
class FactorRDS(RiskDataSource):
    def __init__(self, name, risk_db, table_name, sys_args={}, config_file=None, **kwargs):
        super().__init__(name=name, risk_db=risk_db, table_name=table_name, sys_args=sys_args, config_file=config_file, **kwargs)
        self.FactorNames = self._RiskDB.getTableFactor(table_name)# 数据源中所有的因子名，['LNCAP']
        return
    # 获取 ID, idt: 时点，返回对应该时点的个股风险不缺失的ID序列
    def getID(self, idt):
        iSpecificRisk = self.readSpecificRisk(idt)
        return iSpecificRisk[pd.notnull(iSpecificRisk)].index.tolist()
    # 给定单个时点，提取因子风险矩阵
    def readFactorCov(self, idt, factor_names=None):
        Data = self._RiskDB.readFactorCov(self._TableName, dts=idt)
        if factor_names is not None:
            return Data.ix[factor_names, factor_names]
        else:
            return Data
    # 给定单个时点，提取个股的特别风险
    def readSpecificRisk(self, idt, ids=None):
        return self._RiskDB.readSpecificRisk(self._TableName, dts=idt,ids=ids)
    # 给定单个时点，提取因子截面数据
    def readFactorData(self, idt, factor_names=None, ids=None):
        Data = self._RiskDB.readFactorData(self._TableName, dts=idt, ids=ids)
        if factor_names is not None:
            return Data.ix[:,factor_names]
        else:
            return Data
    # 给定单个时点，提取个股的协方差矩阵
    def readCov(self, idt, ids=None, drop_na=True):
        FactorCov = self.readFactorCov(idt)
        SpecificRisk = self.readSpecificRisk(idt, ids=ids)
        if (FactorCov is None) or (SpecificRisk is None):
            return None
        if ids is None:
            ids = SpecificRisk.index.tolist()
        FactorExpose = self.readFactorData(idt, factor_names=FactorCov.index.tolist(), ids=ids)
        CovMatrix = np.dot(np.dot(FactorExpose.values, FactorCov.values), FactorExpose.values.T) + np.diag(SpecificRisk.values**2)
        if ids is not None:
            CovMatrix = pd.DataFrame(CovMatrix, index=ids, columns=ids)
        else:
            CovMatrix = pd.DataFrame(CovMatrix, index=SpecificRisk.index, columns=SpecificRisk.index)
        if drop_na:
            return dropRiskMatrixNA(CovMatrix)
        return CovMatrix
    # 给定单个时点，提取因子收益率
    def readFactorReturn(self, idt, factor_names=None):
        Data = self._RiskDB.readFactorReturn(self._TableName, dts=idt)
        if factor_names is not None:
            return Data.ix[factor_names]
        else:
            return Data
    # 给定单个时点，提取残余收益率
    def readSpecificReturn(self, idt, ids=None):
        return self._RiskDB.readSpecificReturn(self._TableName, dts=idt, ids=ids)

# 基于mmap的带并行局部缓冲的因子风险数据源, 如果开启遍历模式, 那么限制缓冲的时点长度, 缓冲区里是部分时点序列数据, 如果未开启, 则调用 FactorRDS 提取数据的方法. 适合遍历数据, 内存消耗小, 首次提取时间不长
def _prepareFRDSMMAPCacheData(arg):
    CacheData = {}
    CacheDTs = []
    while True:
        Task = arg["Queue2SubProcess"].get()
        if Task is None:
            break
        if (Task[0] is None) and (Task[1] is None):# 把数据装入缓冲区
            CacheDataByte = pickle.dumps(CacheData)
            DataLen = len(CacheDataByte)
            Msg = None
            if arg["TagName"] is not None:
                MMAPCacheData = mmap.mmap(-1, DataLen, tagname=arg["TagName"])# 当前MMAP缓存区
            else:
                MMAPCacheData = mmap.mmap(-1, DataLen)
                Msg = MMAPCacheData
            MMAPCacheData.seek(0)
            MMAPCacheData.write(CacheDataByte)
            CacheDataByte = None
            arg["Queue2MainProcess"].put((DataLen, Msg))
            Msg = None
            gc.collect()
        else:# 准备缓冲区
            MMAPCacheData = None
            CurInd = Task[0]+arg['ForwardPeriod']+1
            if CurInd<arg["DTNum"]:# 未到结尾处, 需要再准备缓存数据
                OldCacheDTs = CacheDTs
                CacheDTs = arg["DTs"][max(0, CurInd-arg['BackwardPeriod']):min(arg["DTNum"], CurInd+arg['ForwardPeriod']+1)]
                NewCacheDTs = list(set(CacheDTs).difference(set(OldCacheDTs)))
                NewCacheDTs.sort()
                DropDTs = set(OldCacheDTs).difference(set(CacheDTs))
                for iDT in DropDTs:
                    CacheData.pop(iDT)
                FactorCov = arg["RiskDB"].readFactorCov(arg['TableName'], dts=NewCacheDTs)
                SpecificRisk = arg['RiskDB'].readSpecificRisk(arg['TableName'], dts=NewCacheDTs)
                FactorData = arg['RiskDB'].readFactorData(arg['TableName'], dts=NewCacheDTs)
                for iDT in NewCacheDTs:
                    CacheData[iDT] = {}
                    CacheData[iDT]["FactorCov"] = FactorCov[iDT]
                    CacheData[iDT]["SpecificRisk"] = SpecificRisk.loc[iDT]
                    CacheData[iDT]["FactorData"] = FactorData[iDT]
    return 0
class ParaMMAPCacheFRDS(FactorRDS, ParaMMAPCacheRDS):
    def __init__(self, name, risk_db, table_name, sys_args={}, config_file=None, **kwargs):
        ParaMMAPCacheRDS.__init__(self, name=name, risk_db=risk_db, table_name=table_name, sys_args=sys_args, config_file=config_file, **kwargs)
        self.FactorNames = self._RiskDB.getTableFactor(table_name)# 数据源中所有的因子名，['LNCAP']
        self.CacheFun = _prepareFRDSMMAPCacheData
        return
    def start(self, dts, **kwargs):
        return ParaMMAPCacheRDS.start(self, dts, **kwargs)
    def move(self, idt, *args, **kwargs):
        return ParaMMAPCacheRDS.MoveOn(self, idt, *args, **kwargs)
    def end(self):
        return ParaMMAPCacheRDS.end(self)
    def readFactorCov(self, idt, factor_names=None):
        Data = self.CacheData.get(idt)
        if Data is None:# 非遍历模式或者缓冲区无数据
            Data = self._RiskDB.readFactorCov(self._TableName, dts=idt)
        else:
            Data = Data.get("FactorCov")
        if Data is None:
            return None
        if factor_names is not None:
            return Data.ix[factor_names, factor_names]
        else:
            return Data
    def readSpecificRisk(self, idt, ids=None):
        Data = self.CacheData.get(idt)
        if Data is None:# 非遍历模式或者缓冲区无数据
            Data = self._RiskDB.readSpecificRisk(self._TableName, dts=idt, ids=ids)
        else:
            Data = Data.get("SpecificRisk")
        if Data is None:
            return None
        if ids is not None:
            return Data.ix[ids]
        else:
            return Data
    def readFactorData(self, idt, factor_names=None, ids=None):
        Data = self.CacheData.get(idt)
        if Data is None:# 非遍历模式或者缓冲区无数据
            Data = self._RiskDB.readFactorData(self._TableName, dts=idt, ids=ids)
        else:
            Data = Data.get("FactorData")
        if Data is None:
            return None
        if ids is not None:
            Data = Data.ix[ids]
        if factor_names is not None:
            Data = Data.ix[:,factor_names]
        return Data
