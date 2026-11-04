#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Fork 自动同步脚本
读取 repos.txt，逐个调用 GitHub merge-upstream API 把 fork 同步到上游最新。
运行环境：GitHub Actions (ubuntu-latest)，也可本地运行。
"""
import os
import sys
import json
import time
import urllib.request
import urllib.error

OWNER = 'mojinhai'
TOKEN = os.environ.get('SYNC_TOKEN', '')
API = 'https://api.github.com'
HDR = {
    'Authorization': 'token ' + TOKEN,
    'User-Agent': 'fork-sync-bot',
    'Accept': 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
}
HERE = os.path.dirname(os.path.abspath(__file__))


def api(method, path, body=None):
    """调用 GitHub API，返回 (状态码, 解析后的 JSON 或文本)"""
    data = json.dumps(body).encode() if body is not None else None
    h = dict(HDR)
    if data:
        h['Content-Type'] = 'application/json'
    req = urllib.request.Request(API + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw
    except Exception as e:
        return 0, str(e)


def load_manifest():
    """解析清单，返回 (同步列表, 观察列表)"""
    do, watch = [], []
    path = os.path.join(HERE, 'repos.txt')
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split('|')
            if parts[0] == '!watch':
                if len(parts) >= 3:
                    watch.append((parts[1], parts[2]))
            elif len(parts) >= 2:
                do.append((parts[0], parts[1]))
    return do, watch


def upstream_default_branch(upstream):
    """查询上游仓库的默认分支"""
    st, d = api('GET', '/repos/' + upstream)
    if st == 200 and isinstance(d, dict):
        return d.get('default_branch')
    return None


def behind_count(upstream, up_branch, fork, fork_branch):
    """返回 (behind, ahead, status)；查询失败返回 None"""
    st, d = api('GET', '/repos/%s/compare/%s...%s:%s'
                % (upstream, up_branch, OWNER, fork_branch))
    if st == 200 and isinstance(d, dict):
        return d.get('behind_by'), d.get('ahead_by'), d.get('status')
    return None


def sync_one(fork, upstream):
    """同步单个 fork，返回结果状态字符串"""
    # 1. 查上游默认分支
    up_branch = upstream_default_branch(upstream)
    if not up_branch:
        return 'skip', '上游仓库不存在或无法访问'

    # 2. 查 fork 的默认分支
    st, fd = api('GET', '/repos/%s/%s' % (OWNER, fork))
    if st != 200 or not isinstance(fd, dict):
        return 'skip', 'fork 不存在或无法访问'
    fork_branch = fd.get('default_branch')
    if not fork_branch:
        return 'skip', 'fork 无默认分支'

    # 3. 先比较差异
    info = behind_count(upstream, up_branch, fork, fork_branch)
    if info is None:
        return 'skip', '无法比较（可能无共同祖先）'
    behind, ahead, status = info

    if status == 'identical' or (behind == 0 and ahead == 0):
        return 'ok', '已是最新'
    if behind == 0:
        return 'ok', '已是最新（本地有 %d 个自有提交）' % ahead

    # 4. 分叉太多则不动手，只报告
    if status == 'diverged' and ahead > 50:
        return 'watch', '分叉严重（落后 %d / 领先 %d），需人工处理' % (behind, ahead)

    # 5. 调用 merge-upstream
    st, d = api('POST', '/repos/%s/%s/merge-upstream' % (OWNER, fork),
                {'branch': fork_branch})
    if st == 200 and isinstance(d, dict):
        mt = d.get('merge_type', '?')
        if mt == 'none':
            return 'ok', '已是最新'
        return 'ok', '同步成功（%s，落后 %d → 0）' % (mt, behind)
    if st == 409:
        return 'watch', '存在冲突，需人工解决（落后 %d / 领先 %d）' % (behind, ahead)
    if st == 422:
        msg = d.get('message', '') if isinstance(d, dict) else str(d)
        return 'watch', '被拒绝：%s' % msg[:100]
    msg = d.get('message', '') if isinstance(d, dict) else str(d)
    return 'fail', 'HTTP %s：%s' % (st, msg[:100])


def main():
    if not TOKEN:
        print('❌ 缺少 SYNC_TOKEN 环境变量')
        sys.exit(1)

    do_list, watch_list = load_manifest()
    results = {'ok': [], 'skip': [], 'watch': [], 'fail': []}

    print('=' * 62)
    print('GitHub Fork 自动同步  |  共 %d 个待同步 + %d 个观察' %
          (len(do_list), len(watch_list)))
    print('=' * 62)

    for fork, upstream in do_list:
        print('\n▶ %s  ←  %s' % (fork, upstream))
        try:
            status, msg = sync_one(fork, upstream)
        except Exception as e:
            status, msg = 'fail', '异常：%s' % str(e)[:100]
        icon = {'ok': '✅', 'skip': '⏭️', 'watch': '👀', 'fail': '❌'}[status]
        print('  %s %s' % (icon, msg))
        results[status].append((fork, msg))
        time.sleep(1)

    # 观察区只查差异
    if watch_list:
        print('\n' + '-' * 62)
        print('👀 观察区（不做任何改动）')
        print('-' * 62)
        for fork, upstream in watch_list:
            up_branch = upstream_default_branch(upstream)
            if not up_branch:
                print('  ⚠️ %-24s 上游不存在' % fork)
                continue
            st, fd = api('GET', '/repos/%s/%s' % (OWNER, fork))
            if st != 200 or not isinstance(fd, dict):
                print('  ⚠️ %-24s fork 无法访问' % fork)
                continue
            info = behind_count(upstream, up_branch, fork, fd.get('default_branch'))
            if info is None:
                print('  ⚠️ %-24s 无法比较（无共同祖先）' % fork)
            else:
                b, a, s = info
                print('  👀 %-24s 落后 %-6s 领先 %-6s %s' % (fork, b, a, s))
            time.sleep(0.8)

    # 汇总
    print('\n' + '=' * 62)
    print('📊 汇总：成功 %d | 跳过 %d | 需人工 %d | 失败 %d'
          % (len(results['ok']), len(results['skip']),
             len(results['watch']), len(results['fail'])))
    print('=' * 62)

    # 有失败则退出码非 0，让 Actions 标红
    if results['fail']:
        sys.exit(1)


if __name__ == '__main__':
    main()