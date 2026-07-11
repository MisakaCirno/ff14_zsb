# 主站前端

本目录只构建 Django 主站使用的 TypeScript、HTMX、Alpine 和 CSS。

明确不属于此管线的目录：

- `../static/overlay/`
- `../static/viewer_new/`
- `../sb_renderer/`

首次安装：

```powershell
npm --prefix frontend ci
```

验证和生产构建：

```powershell
npm --prefix frontend run verify
```

开发监听构建：

```powershell
npm --prefix frontend run dev
```

输出目录为 `../static/app/`，该目录是生成物且不进入 Git。Vite manifest 固定输出为 `static/app/manifest.json`，供 Django 模板加载带哈希的入口文件。
