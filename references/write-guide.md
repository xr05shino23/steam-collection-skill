# 步骤4：收藏集生成与写入 Steam

## 写入目标

新版 Steam 客户端把用户自建收藏集存在**本地云存储文件**中：

```
<steam_root>/userdata/<steamid32>/config/cloudstorage/cloud-storage-namespace-1.json
```

steamid32 与 steamid64 的换算：`steamid64 = 76561197960265728 + steamid32`。
steam_root 默认 `C:\Program Files (x86)\Steam`，以注册表 `HKCU\Software\Valve\Steam\SteamPath` 为准。

## steam_collections.json 格式

目标文件是一个 JSON **数组**，每个元素为 `[key, meta]` 二元组。收藏集条目：

```json
[
  ["user-collections.<cid>", {
    "key": "user-collections.<cid>",
    "timestamp": 1700000000,
    "value": "{\"id\":\"<cid>\",\"name\":\"C06 模拟经营\",\"added\":[220,400,570],\"removed\":[]}",
    "version": "2658",
    "conflictResolutionMethod": "custom",
    "strMethodId": "union-collections"
  }]
]
```

- `<cid>` 生成方式：`base64( sha1( "<盐>:" + 类目编码 ).digest()[:9] )`，盐用用户自定义字符串
  （每个用户固定一个即可；同一盐+编码生成的 id 稳定，重复写入不会产生重复收藏集）
- `added` 是该收藏集成员 appid 的**整数**数组，升序去重
- `value` 是**字符串化的 JSON**（不是嵌套对象），注意转义
- 文件里除收藏集外还有其他条目（截图/设置等），写入时**只增删 `user-collections.*` 前缀的 key**，
  其余原样保留

## 写入协议（write_steam.py 实现）

```
1. Steam 运行守卫: PowerShell Get-Process steam,steamwebhelper,steamservice + 注册表
   ActiveUser + tasklist 三重交叉检测; 运行中或无法判定 -> REFUSE (用户可 --force 但必须先警告)
2. 校验目标文件存在且为 JSON 数组
3. 移除全部旧 "user-collections.*" 条目 -> 合并新条目 -> 校验 key 无重复
4. 序列化(ensure_ascii=False, 紧凑分隔符) -> 再 json.loads 校验合法性
5. --dry-run 到此为止
6. 双备份: 项目 backup/ 一份 + 目标文件同级 <原名>_backup_<时间戳>.json 一份
7. 写临时文件 -> 再校验 -> os.replace 原子替换
8. 记录 state/last_write.json
```

## 写前确认清单（向用户展示，必须全部得到肯定答复）

1. 将移除旧收藏集 **N** 个、写入新收藏集 **M** 个、文件其余条目原样保留
2. 备份位置（两个路径）
3. Steam 已完全退出（含托盘 steamwebhelper/steamservice）
4. 用户明确回复"写入/确认"后才执行

## 写入后

- 提示用户启动 Steam，在库左上"筛选器"中查看收藏集是否出现（首次同步可能要等几分钟或重启一次）
- 告知回滚方法：退出 Steam → 用备份 JSON 覆盖回目标文件 → 启动 Steam
- 生成历史快照（分类表 + 收藏集 + 规则）便于日后追溯

## 常见失败

| 现象 | 处理 |
|---|---|
| REFUSE: Steam 正在运行 | 托盘彻底退出再试；确认已退出可 --force |
| REFUSE: 找不到目标文件 | steamid32 或 steam_root 配错；确认 userdata 下实际目录名 |
| Steam 启动后收藏集没出现 | 等云同步/重启 Steam；检查写入的 JSON 是否合法（key 重复会导致客户端忽略） |
| 收藏集体量过大难管理 | 类目设计阶段控制大类数量；兜底类成员过多时增拆细分厂商标识 |
