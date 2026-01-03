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