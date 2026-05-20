#!/usr/bin/env python3

import os
import argparse

def export_file_list(source_dir, output_file):
    """递归遍历文件夹，将每个文件的相对路径写入输出文件"""
    source_dir = os.path.abspath(source_dir)
    files = []
    for root, dirs, filenames in os.walk(source_dir):
        for fname in filenames:
            full_path = os.path.join(root, fname)
            rel_path = os.path.relpath(full_path, source_dir)
            # 统一使用正斜杠，避免跨平台问题
            files.append(rel_path.replace(os.sep, '/'))
    
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(sorted(files)))
    print(f"✅ 已导出 {len(files)} 个文件路径到: {output_file}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="导出文件夹内所有文件的相对路径清单")
    parser.add_argument("source", help="源文件夹路径，比如 D:\\Project\\A")
    parser.add_argument("-o", "--output", default="filelist.txt", help="输出清单文件路径（默认 filelist.txt）")
    args = parser.parse_args()
    export_file_list(args.source, args.output)
