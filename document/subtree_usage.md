# 初始化subtree
```
git remote add sb_renderer https://github.com/FairyScript/ffxiv-stratboard-react.git
git fetch sb_renderer
git subtree add --prefix=sb_renderer sb_renderer master
git status
```

# 更新subtree
```
git fetch sb_renderer
git subtree pull --prefix=sb_renderer sb_renderer master
```

# 部署环境
## 安装 bun
`powershell -c "irm bun.sh/install.ps1 | iex"`
若网络环境不佳，可让尝试让控制台走代理：
`$Env:http_proxy="http://127.0.0.1:7890";`
`$Env:https_proxy="http://127.0.0.1:7890";`

## 构建项目
```
bun i 
bun run build
```