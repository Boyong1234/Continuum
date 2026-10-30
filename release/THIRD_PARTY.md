# 第三方与项目许可状态

Continuum 自有源码采用 AGPL-3.0-or-later（GNU Affero General Public License v3 或任何后续版本），完整、未经修改的许可证正文见项目根目录 LICENSE，来源为 AstrBot v4.29.3 上游许可证。项目许可证不替代第三方组件自身的许可证及版权声明。

- AstrBot固定4.29.3，AGPL-3.0-or-later；保留上游许可文本`AstrBot-LICENSE.txt`与[对应版本源码](https://github.com/AstrBotDevs/AstrBot/tree/v4.29.3)。本包只提供Bridge源码和依赖锁，不捆绑AstrBot发行二进制；插件与框架结合的发布需遵守适用义务。
- SnowLuma固定参考2.2.1，[官方EULA](https://github.com/SnowLuma/SnowLuma/blob/v2.2.1/EULA.md)第5节限制专有组件复制、打包、自动部署。本包不含SnowLuma代码、发行包、原生组件或登录/管理状态。用户自行从官方允许渠道准备，不自动安装/注入或接受条款。
- 官方QQ由用户从[官方网站](https://im.qq.com/)安装及扫码，不复制QQ安装目录或AppData。
- CPython许可见[官方说明](https://docs.python.org/3.12/license.html)。公开源码包不含现机Runtime、VC DLL或Python二进制；用户安装官方Python3.12 x64。私人包的现有Runtime仅供原用户私人迁移，保留许可证和dist-info；不据此宣布其中全部第三方DLL已获公开分发许可。
- Python及前端依赖分别见backend锁文件、release/requirements-astrbot.txt、frontend/pnpm-lock.yaml；安装时保留各依赖license/NOTICE。AstrBot依赖中部分现有dist-info缺失许可材料，公开源码不捆绑它们，未来二进制Release需另行核实。

非官方QQ接入存在平台风控/失效风险，不承诺免封号或跨机免扫码。
