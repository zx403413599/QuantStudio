# coding=utf-8
"""基于 csv 文件的因子库"""
import os
import stat
import shutil
import pickle
import shelve
import datetime as dt

import numpy as np
import pandas as pd
import fasteners
from traits.api import Directory

from QuantStudio import __QS_Error__, __QS_ConfigPath__
from QuantStudio.FactorDataBase.FactorDB import WritableFactorDB, FactorTable
from QuantStudio.Tools.FileFun import listDirDir, listDirFile, readJSONFile
from QuantStudio.Tools.DataTypeFun import readNestedDictFromHDF5, writeNestedDict2HDF5

def _identifyDataType(factor_data, data_type=None):
    if (data_type is None) or (data_type=="double"):
        try:
            factor_data = factor_data.astype(float)
        except:
            data_type = "object"
        else:
            data_type = "double"
    return (factor_data, data_type)

def _adjustData(data, data_type, order="C"):
    if data_type=="string": return data.where(pd.notnull(data), None).values
    elif data_type=="double": return data.astype("float").values
    elif data_type=="object":
        if order=="C":
            return np.ascontiguousarray(data.applymap(lambda x: np.frombuffer(pickle.dumps(x), dtype=np.uint8)).values)
        elif order=="F":
            return np.asfortranarray(data.applymap(lambda x: np.frombuffer(pickle.dumps(x), dtype=np.uint8)).values)
        else:
            raise __QS_Error__("不支持的参数 order 值: %s" % order)
    else: raise __QS_Error__("不支持的数据类型: %s" % data_type)

class _FactorTable(FactorTable):
    """CSVDB 因子表"""
    def __init__(self, name, fdb, sys_args={}, **kwargs):
        self._Suffix = fdb._Suffix# 文件后缀名
        return super().__init__(name=name, fdb=fdb, sys_args=sys_args, **kwargs)
    @property
    def FactorNames(self):
        return sorted(listDirFile(self._FactorDB.MainDir+os.sep+self.Name, suffix=self._Suffix))
    def getMetaData(self, key=None, args={}):
        with self._FactorDB._getLock(self._Name):
            with shelve.open(self._FactorDB.MainDir+os.sep+self.Name+os.sep+"_TableInfo") as File:
                if key is not None:
                    return File[key]
                else:
                    return dict(File)
    def getFactorMetaData(self, factor_names=None, key=None, args={}):
        raise NotImplementedError
    def getID(self, ifactor_name=None, idt=None, args={}):
        if ifactor_name is None: ifactor_name = self.FactorNames[0]
        with self._FactorDB._getLock(self._Name):
            with open(self._FactorDB.MainDir+os.sep+self.Name+os.sep+ifactor_name+"."+self._Suffix, mode="r") as ijFile:
                return sorted(ijFile.readline().strip().split(",")[1:])
    def getDateTime(self, ifactor_name=None, iid=None, start_dt=None, end_dt=None, args={}):
        if ifactor_name is None: ifactor_name = self.FactorNames[0]
        with self._FactorDB._getLock(self._Name):
            with open(self._FactorDB.MainDir+os.sep+self.Name+os.sep+ifactor_name+"."+self._Suffix, mode="r") as ijFile:
                Timestamps = ijFile["DateTime"][...]
        if start_dt is not None:
            if isinstance(start_dt, pd.Timestamp) and (pd.__version__>="0.20.0"): start_dt = start_dt.to_pydatetime().timestamp()
            else: start_dt = start_dt.timestamp()
            Timestamps = Timestamps[Timestamps>=start_dt]
        if end_dt is not None:
            if isinstance(end_dt, pd.Timestamp) and (pd.__version__>="0.20.0"): end_dt = end_dt.to_pydatetime().timestamp()
            else: end_dt = end_dt.timestamp()
            Timestamps = Timestamps[Timestamps<=end_dt]
        return sorted(dt.datetime.fromtimestamp(iTimestamp) for iTimestamp in Timestamps)
    def __QS_calcData__(self, raw_data, factor_names, ids, dts, args={}):
        Data = {iFactor: self.readFactorData(ifactor_name=iFactor, ids=ids, dts=dts, args=args) for iFactor in factor_names}
        return pd.Panel(Data).loc[factor_names]
    def readFactorData(self, ifactor_name, ids, dts, args={}):
        FilePath = self._FactorDB.MainDir+os.sep+self.Name+os.sep+ifactor_name+"."+self._Suffix
        if not os.path.isfile(FilePath): raise __QS_Error__("因子库 '%s' 的因子表 '%s' 中不存在因子 '%s'!" % (self._FactorDB.Name, self.Name, ifactor_name))
        with self._FactorDB._getLock(self._Name):
            with h5py.File(FilePath, mode="r") as DataFile:
                DataType = DataFile.attrs["DataType"]
                DateTimes = DataFile["DateTime"][...]
                IDs = DataFile["ID"][...]
                if dts is None:
                    if ids is None:
                        Rslt = pd.DataFrame(DataFile["Data"][...], index=DateTimes, columns=IDs).sort_index(axis=1)
                    elif set(ids).isdisjoint(IDs):
                        Rslt = pd.DataFrame(index=DateTimes, columns=ids)
                    else:
                        Rslt = pd.DataFrame(DataFile["Data"][...], index=DateTimes, columns=IDs).loc[:, ids]
                    Rslt.index = [dt.datetime.fromtimestamp(itms) for itms in Rslt.index]
                elif (ids is not None) and set(ids).isdisjoint(IDs):
                    Rslt = pd.DataFrame(index=dts, columns=ids)
                else:
                    if dts and isinstance(dts[0], pd.Timestamp) and (pd.__version__>="0.20.0"): dts = [idt.to_pydatetime().timestamp() for idt in dts]
                    else: dts = [idt.timestamp() for idt in dts]
                    DateTimes = pd.Series(np.arange(0, DateTimes.shape[0]), index=DateTimes, dtype=np.int)
                    DateTimes = DateTimes[DateTimes.index.intersection(dts)]
                    nDT = DateTimes.shape[0]
                    if nDT==0:
                        if ids is None: Rslt = pd.DataFrame(index=dts, columns=IDs).sort_index(axis=1)
                        else: Rslt = pd.DataFrame(index=dts, columns=ids)
                    elif nDT<1000:
                        DateTimes = DateTimes.sort_values()
                        Mask = DateTimes.tolist()
                        DateTimes = DateTimes.index.values
                        if ids is None:
                            Rslt = pd.DataFrame(DataFile["Data"][Mask, :], index=DateTimes, columns=IDs).loc[dts].sort_index(axis=1)
                        else:
                            IDRuler = pd.Series(np.arange(0,IDs.shape[0]), index=IDs)
                            IDRuler = IDRuler.loc[ids]
                            StartInd, EndInd = int(IDRuler.min()), int(IDRuler.max())
                            Rslt = pd.DataFrame(DataFile["Data"][Mask, StartInd:EndInd+1], index=DateTimes, columns=IDs[StartInd:EndInd+1]).loc[dts, ids]
                    else:
                        Rslt = pd.DataFrame(DataFile["Data"][...], index=DataFile["DateTime"][...], columns=IDs).loc[dts]
                        if ids is not None: Rslt = Rslt.loc[:, ids]
                        else: Rslt.sort_index(axis=1, inplace=True)
                    Rslt.index = [dt.datetime.fromtimestamp(itms) for itms in Rslt.index]
        if DataType=="string":
            Rslt = Rslt.where(pd.notnull(Rslt), None)
            Rslt = Rslt.where(Rslt!="", None)
        elif DataType=="object":
            Rslt = Rslt.applymap(lambda x: pickle.loads(bytes(x)) if isinstance(x, np.ndarray) and (x.shape[0]>0) else None)
        return Rslt.sort_index(axis=0)

# 基于 CSV 文件的因子数据库
# 每一张表是一个文件夹, 每个因子是一个 CSV 文件
# 每个 CSV 文件第一行为 ID, 第一列为 DateTime, 第一行第一列为 DataType, 其余为 Data;
# 表的元数据存储在表文件夹下特殊文件: _TableInfo 中
class CSVDB(WritableFactorDB):
    """CSVDB"""
    MainDir = Directory(label="主目录", arg_type="Directory", order=0)
    def __init__(self, sys_args={}, config_file=None, **kwargs):
        self._LockFile = None# 文件锁的目标文件
        self._DataLock = None# 访问该因子库资源的锁, 防止并发访问冲突
        self._isAvailable = False
        self._Suffix = "csv"# 文件的后缀名
        super().__init__(sys_args=sys_args, config_file=(__QS_ConfigPath__+os.sep+"CSVDBConfig.json" if config_file is None else config_file), **kwargs)
        # 继承来的属性
        self.Name = "CSVDB"
        return
    def __getstate__(self):
        state = self.__dict__.copy()
        # Remove the unpicklable entries.
        state["_DataLock"] = (True if self._DataLock is not None else False)
        return state
    def __setstate__(self, state):
        super().__setstate__(state)
        if self._DataLock:
            self._DataLock = fasteners.InterProcessLock(self._LockFile)
        else:
            self._DataLock = None
    def connect(self):
        if not os.path.isdir(self.MainDir):
            raise __QS_Error__("CSVDB.connect: 不存在主目录 '%s'!" % self.MainDir)
        self._LockFile = self.MainDir+os.sep+"LockFile"
        if not os.path.isfile(self._LockFile):
            open(self._LockFile, mode="a").close()
            os.chmod(self._LockFile, stat.S_IRWXO | stat.S_IRWXG | stat.S_IRWXU)
        self._DataLock = fasteners.InterProcessLock(self._LockFile)
        self._isAvailable = True
        return 0
    def disconnect(self):
        self._LockFile = None
        self._DataLock = None
        self._isAvailable = False
    def isAvailable(self):
        return self._isAvailable
    def _getLock(self, table_name=None):
        if table_name is None:
            return self._DataLock
        TablePath = self.MainDir + os.sep + table_name
        if not os.path.isdir(TablePath):
            Msg = ("因子库 '%s' 调用 _getLock 时错误, 不存在因子表: '%s'" % (self.Name, table_name))
            self._QS_Logger.error(Msg)
            raise __QS_Error__(Msg)
        LockFile = TablePath + os.sep + "LockFile"
        if not os.path.isfile(LockFile):
            with self._DataLock:
                if not os.path.isfile(LockFile):
                    open(LockFile, mode="a").close()
                    os.chmod(LockFile, stat.S_IRWXO | stat.S_IRWXG | stat.S_IRWXU)
        return fasteners.InterProcessLock(LockFile)
    # -------------------------------表的操作---------------------------------
    @property
    def TableNames(self):
        return sorted(listDirDir(self.MainDir))
    def getTable(self, table_name, args={}):
        if not os.path.isdir(self.MainDir+os.sep+table_name): raise __QS_Error__("CSVDB.getTable: 表 '%s' 不存在!" % table_name)
        return _FactorTable(name=table_name, fdb=self, sys_args=args, logger=self._QS_Logger)
    def renameTable(self, old_table_name, new_table_name):
        if old_table_name==new_table_name: return 0
        OldPath = self.MainDir+os.sep+old_table_name
        NewPath = self.MainDir+os.sep+new_table_name
        with self._DataLock:
            if not os.path.isdir(OldPath): raise __QS_Error__("CSVDB.renameTable: 表: '%s' 不存在!" % old_table_name)
            if os.path.isdir(NewPath): raise __QS_Error__("CSVDB.renameTable: 表 '"+new_table_name+"' 已存在!")
            os.rename(OldPath, NewPath)
        return 0
    def deleteTable(self, table_name):
        TablePath = self.MainDir+os.sep+table_name
        with self._DataLock:
            if os.path.isdir(TablePath):
                shutil.rmtree(TablePath, ignore_errors=True)
        return 0
    def setTableMetaData(self, table_name, key=None, value=None, meta_data=None):
        with self._DataLock:
            with shelve.open(self.MainDir+os.sep+table_name+os.sep+"_TableInfo") as File:
                if key is not None:
                    if value is not None:
                        File[key] = value
                    else:
                        del File[key]
        if meta_data is not None:
            for iKey in meta_data:
                self.setTableMetaData(table_name, key=iKey, value=meta_data[iKey], meta_data=None)
        return 0
    # ----------------------------因子操作---------------------------------
    def renameFactor(self, table_name, old_factor_name, new_factor_name):
        if old_factor_name==new_factor_name: return 0
        OldPath = self.MainDir+os.sep+table_name+os.sep+old_factor_name+"."+self._Suffix
        NewPath = self.MainDir+os.sep+table_name+os.sep+new_factor_name+"."+self._Suffix
        with self._DataLock:
            if not os.path.isfile(OldPath): raise __QS_Error__("CSVDB.renameFactor: 表 ’%s' 中不存在因子 '%s'!" % (table_name, old_factor_name))
            if os.path.isfile(NewPath): raise __QS_Error__("CSVDB.renameFactor: 表 ’%s' 中的因子 '%s' 已存在!" % (table_name, new_factor_name))
            os.rename(OldPath, NewPath)
        return 0
    def deleteFactor(self, table_name, factor_names):
        TablePath = self.MainDir+os.sep+table_name
        FactorNames = set(listDirFile(TablePath, suffix=self._Suffix))
        with self._DataLock:
            if FactorNames.issubset(set(factor_names)):
                shutil.rmtree(TablePath, ignore_errors=True)
            else:
                for iFactor in factor_names:
                    iFilePath = TablePath+os.sep+iFactor+"."+self._Suffix
                    if os.path.isfile(iFilePath):
                        os.remove(iFilePath)
        return 0
    def setFactorMetaData(self, table_name, ifactor_name, key=None, value=None, meta_data=None):
        raise NotImplementedError
    def writeFactorData(self, factor_data, table_name, ifactor_name, if_exists="update", data_type=None):
        raise NotImplementedError
    def writeData(self, data, table_name, if_exists="update", data_type={}, **kwargs):
        for i, iFactor in enumerate(data.items):
            self.writeFactorData(data.iloc[i], table_name, iFactor, if_exists=if_exists, data_type=data_type.get(iFactor, None))
        return 0
