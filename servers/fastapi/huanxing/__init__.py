"""唤星（HuanXing）对 Presenton FastAPI 的接入期补丁包（embedded_desktop 形态）。

本包只承载唤星侧逻辑（安全加固等），上游业务源码保持纯净；接入仅在
`api/main.py` 唯一装配点接线（构建期可重放，便于跟随上游 fork rebase）。
"""
