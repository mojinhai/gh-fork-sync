#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Fork 自动同步脚本（全自动发现版）

工作方式：
1. 调用 API 拉取 mojinhai 名下所有仓库
2. 筛选出所有 fork，并逐个查询其上游 parent 仓库
3. 排除 skip.txt 里列出的仓库（黑名单）
4. 对剩余 fork 逐个执行 merge-upstream 同步

你新 fork 任何项目，脚本第二天会自动发现并同步，无需手动维护清单。
"""
import os
import sys
import json
import time
import re
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

# 自动产生的提交特征：命中则视为「非用户手写」，可安全继续合并
AUTO_PATTERNS = [
    re.compile(r'^chore:\s*sync from upstream', re.I),
    re.compile(r'^Merge (branch|remote-tracking branch|pull request)', re.I),
    re.compile(r"^Merge branch '.*' into .*", re.I),
    re.compile(r'^Merge .+ into .+', re.I),
]

# 默认跳过名单（即使写在 skip.txt 里也会叠加）
DEFAULT_SKIP = {
    'Kwrt',                    # 自己改过配置，分叉严重
    'immortalwrt-mt798x-6.6',  # 自建分支，非标准 fork 关系
    'OpenWRT-Mediatek',        # 上游仓库已删除
    'Xray_bash_onekey',        # 与上游无共同祖先
}


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


def load_skip():
    """读取 skip.txt 黑名单，与默认名单合并"""
    skip = set(DEFAULT_SKIP)
    path = os.path.join(HERE, 'skip.txt')
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    skip.add(line)
    return skip


def discover_forks():
    """自动发现所有 fork，返回 [(fork名, 上游owner/repo, 默认分支)]"""
    found = []
    page = 1
    while True:
        st, repos = api('GET', '/user/repos?per_page=100&page=%d'
                        '&affiliation=owner&sort=updated' % page)
        if st != 200 or not isinstance(repos, list):
            print('  ⚠️ 第 %d 页拉取失败：%s' % (page, st))
            break
        if not repos:
            break
        for r in repos:
            if not r.get('fork'):
                continue
            name = r.get('name')
            # 列表接口不返回 parent，必须单独查
            st2, d = api('GET', '/repos/%s/%s' % (OWNER, name))
            if st2 != 200 or not isinstance(d, dict):
                continue
            parent = (d.get('parent') or {}).get('full_name')
            if not parent:
                continue
            found.append((name, parent, d.get('default_branch')))
            time.sleep(0.3)
        if len(repos) < 100:
            break
        page += 1
    return found


def is_auto_commit(msg):
    """判断提交信息是否属于自动产生（非用户手写）"""
    first = msg.split('\n')[0].strip()
    return any(p.search(first) for p in AUTO_PATTERNS)


def compare(upstream, up_branch, fork_branch):
    """返回 compare 结果 dict，失败返回 None"""
    st, d = api('GET', '/repos/%s/compare/%s...%s:%s'
                % (upstream, up_branch, OWNER, fork_branch))
    if st == 200 and isinstance(d, dict):
        return d
    return None


def has_manual_commits(fork, fork_branch, upstream):
    """检查 fork 上是否存在用户手写的提交（不在上游的）"""
    st, recent = api('GET', '/repos/%s/commits?sha=%s&per_page=30'
                     % (fork, fork_branch))
    if st != 200 or not isinstance(recent, list):
        return None  # 查询失败，未知
    for cm in recent:
        sha = cm.get('sha')
        st2, _ = api('GET', '/repos/%s/commits/%s' % (upstream, sha))
        if st2 == 200:
            continue  # 上游已有此提交
        msg = cm.get('commit', {}).get('message', '')
        if not is_auto_commit(msg):
            return msg.split('\n')[0][:50]
    return None


def sync_one(fork, upstream, fork_branch):
    """同步单个 fork，返回 (状态, 说明)"""
    st, u = api('GET', '/repos/' + upstream)
    if st != 200 or not isinstance(u, dict):
        return 'skip', '上游仓库不存在或无法访问'
    up_branch = u.get('default_branch')
    if not fork_branch:
        return 'skip', 'fork 无默认分支'

    c = compare(upstream, up_branch, fork_branch)
    if c is None:
        return 'skip', '无法比较（可能无共同祖先）'
    behind = c.get('behind_by', 0)
    ahead = c.get('ahead_by', 0)
    status = c.get('status')

    if status == 'identical' or (behind == 0 and ahead == 0):
        return 'ok', '已是最新'
    if behind == 0:
        return 'ok', '已是最新（本地领先 %d 个提交）' % ahead

    # 分叉状态：检查是否有手写提交
    if status == 'diverged' and ahead > 0:
        manual = has_manual_commits(fork, fork_branch, upstream)
        if manual:
            return 'watch', ('有手写提交（%s），落后 %d 需人工处理'
                             % (manual, behind))

    st, d = api('POST', '/repos/%s/%s/merge-upstream' % (OWNER, fork),
                {'branch': fork_branch})
    if st == 200 and isinstance(d, dict):
        mt = d.get('merge_type', '?')
        if mt == 'none':
            return 'ok', '已是最新'
        return 'ok', '同步成功（%s，落后 %d → 0）' % (mt, behind)
    if st == 409:
        return 'watch', '存在冲突（落后 %d / 领先 %d）' % (behind, ahead)
    if st == 422:
        msg = d.get('message', '') if isinstance(d, dict) else str(d)
        return 'watch', '被拒绝：%s' % str(msg)[:100]
    msg = d.get('message', '') if isinstance(d, dict) else str(d)
    return 'fail', 'HTTP %s：%s' % (st, str(msg)[:100])


def main():
    if not TOKEN:
        print('❌ 缺少 SYNC_TOKEN 环境变量')
        sys.exit(1)

    print('=' * 62)
    print('GitHub Fork 自动同步（自动发现模式）')
    print('=' * 62)

    print('\n🔍 正在扫描你的仓库...')
    all_forks = discover_forks()
    print('   发现 %d 个 fork' % len(all_forks))

    skip = load_skip()
    do_list, watch_list = [], []
    for name, parent, branch in all_forks:
        if name in skip:
            watch_list.append((name, parent, branch))
        else:
            do_list.append((name, parent, branch))

    print('   待同步 %d 个，跳过 %d 个' % (len(do_list), len(watch_list)))

    results = {'ok': [], 'skip': [], 'watch': [], 'fail': []}

    for fork, upstream, branch in do_list:
        print('\n▶ %s  ←  %s' % (fork, upstream))
        try:
            status, msg = sync_one(fork, upstream, branch)
        except Exception as e:
            status, msg = 'fail', '异常：%s' % str(e)[:100]
        icon = {'ok': '✅', 'skip': '⏭️', 'watch': '👀', 'fail': '❌'}[status]
        print('  %s %s' % (icon, msg))
        results[status].append((fork, msg))
        time.sleep(1)

    if watch_list:
        print('\n' + '-' * 62)
        print('👀 跳过名单（不做改动）')
        print('-' * 62)
        for fork, upstream, branch in watch_list:
            st, u = api('GET', '/repos/' + upstream)
            if st != 200 or not isinstance(u, dict):
                print('  ⚠️ %-24s 上游已不存在' % fork)
                continue
            c = compare(upstream, u.get('default_branch'), branch)
            if c is None:
                print('  ⚠️ %-24s 无法比较（无共同祖先）' % fork)
            else:
                print('  👀 %-24s 落后 %-6s 领先 %-6s'
                      % (fork, c.get('behind_by'), c.get('ahead_by')))
            time.sleep(0.6)

    print('\n' + '=' * 62)
    print('📊 汇总：成功 %d | 跳过 %d | 需人工 %d | 失败 %d'
          % (len(results['ok']), len(results['skip']),
             len(results['watch']), len(results['fail'])))
    print('=' * 62)

    if results['fail']:
        sys.exit(1)


if __name__ == '__main__':
    main()