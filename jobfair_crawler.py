#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import requests
import json
import re

def search_for_enterprises():
    """搜索招聘会企业信息"""
    print("开始搜索杭州青年人才交流大会企业信息...")
    
    # 从已知文章中提取的企业
    known_enterprises = [
        "西湖大学",
        "中科院医学所", 
        "蚂蚁科技集团",
        "海康威视",
        "宇树科技",
        "云深处"
    ]
    
    # 尝试搜索更多相关信息
    search_terms = [
        "杭州 招聘会 企业 名单 2026",
        "起跑春天 参会企业",
        "杭州大会展中心 招聘 企业"
    ]
    
    print("已知参会企业（来自新闻报道）：")
    for i, enterprise in enumerate(known_enterprises, 1):
        print(f"{i}. {enterprise}")
    
    # 尝试从其他来源获取更多企业信息
    print("\n尝试获取更多企业信息...")
    
    # 这里可以添加更多搜索逻辑
    # 由于网站可能有反爬虫机制，我们主要依赖公开报道
    
    return known_enterprises

def get_more_enterprises_from_articles():
    """从相关文章中提取更多企业信息"""
    articles = [
        {
            "url": "https://www.zj.chinanews.com.cn/cj/2026-03-06/detail-ihfaimzs0649597.shtml",
            "content": """参加本次活动的招聘单位，既有西湖大学、中科院医学所等重点科研平台，也有蚂蚁科技集团、海康威视等行业龙头企业，还有宇树科技、云深处等新兴科技企业同台纳贤，汇聚了杭州产业发展的"中流砥柱"与"新兴力量"。
            
            各单位的招聘岗位覆盖全面，类型多元，既有算法工程师、模拟电路设计工程师、研发工程师等高端研发岗位，也有技术操作、运营管理、行政文秘、市场营销等众多职能型岗位。"""
        }
    ]
    
    enterprises = []
    
    for article in articles:
        # 提取企业名称（简单正则匹配）
        text = article["content"]
        
        # 常见的企业名称模式
        patterns = [
            r'[A-Za-z0-9\u4e00-\u9fa5]{2,20}(科技|集团|公司|大学|研究所|医院|银行|证券|保险)',
            r'(蚂蚁|阿里|腾讯|百度|华为|字节|京东|美团|滴滴|拼多多|网易|小米|快手|哔哩哔哩|B站)'
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, text)
            for match in matches:
                if isinstance(match, tuple):
                    match = match[0]
                if match not in enterprises:
                    enterprises.append(match)
    
    return enterprises

def main():
    """主函数"""
    print("=" * 60)
    print("杭州青年人才交流大会企业信息爬取工具")
    print("=" * 60)
    
    # 获取企业信息
    enterprises = search_for_enterprises()
    
    # 尝试从文章中提取更多企业
    more_enterprises = get_more_enterprises_from_articles()
    
    # 合并去重
    all_enterprises = list(set(enterprises + more_enterprises))
    
    print(f"\n总共找到 {len(all_enterprises)} 家参会企业：")
    for i, enterprise in enumerate(sorted(all_enterprises), 1):
        print(f"{i}. {enterprise}")
    
    # 保存到文件
    with open("hangzhou_jobfair_enterprises.txt", "w", encoding="utf-8") as f:
        f.write("2026年'起跑春天'杭州青年人才交流大会参会企业名单\n")
        f.write("=" * 50 + "\n\n")
        for i, enterprise in enumerate(sorted(all_enterprises), 1):
            f.write(f"{i}. {enterprise}\n")
    
    print(f"\n企业名单已保存到 hangzhou_jobfair_enterprises.txt")

if __name__ == "__main__":
    main()