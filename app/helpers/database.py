#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
helpers/database.py
Utilitários para gerenciar o banco de dados PostgreSQL/Supabase
"""

from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
import json
from supabase import create_client
import os

# ═══════════════════════════════════════════════════════════════════════════

class DatabaseManager:
    """Gerenciador de operações do banco de dados"""
    
    def __init__(self):
        """Inicializa conexão com Supabase"""
        self.url = os.getenv('SUPABASE_URL')
        self.key = os.getenv('SUPABASE_KEY')
        self.client = create_client(self.url, self.key)
        self.db = self.client.postgrest
    
    # ─────────────────────────────────────────────────────────────────────────
    # TENANT OPERATIONS
    # ─────────────────────────────────────────────────────────────────────────
    
    def criar_tenant(self, dados: Dict[str, Any]) -> Dict:
        """
        Criar um novo tenant (empresa)
        
        Args:
            dados: {
                'slug': 'meu-negocio',
                'nome': 'Meu Negócio LTDA',
                'tipo': 'restaurante',
                'cnpj': '12.345.678/0001-90',
                'email': 'contato@meu-negocio.com'
            }
        
        Returns:
            Dados do tenant criado
        """
        try:
            response = self.db.table('tenants').insert(dados).execute()
            return response.data[0] if response.data else None
        except Exception as e:
            print(f"Erro ao criar tenant: {e}")
            return None
    
    def listar_tenants(self, ativo_apenas: bool = True) -> List[Dict]:
        """
        Listar all tenants
        
        Args:
            ativo_apenas: Se True, lista apenas ativos
        
        Returns:
            Lista de tenants
        """
        try:
            query = self.db.table('tenants')
            if ativo_apenas:
                query = query.eq('ativo', True)
            response = query.select('*').execute()
            return response.data
        except Exception as e:
            print(f"Erro ao listar tenants: {e}")
            return []
    
    def get_tenant_by_slug(self, slug: str) -> Optional[Dict]:
        """Obter tenant por slug"""
        try:
            response = self.db.table('tenants').eq('slug', slug).select('*').execute()
            return response.data[0] if response.data else None
        except Exception as e:
            print(f"Erro ao buscar tenant: {e}")
            return None
    
    def atualizar_tenant(self, tenant_id: str, dados: Dict) -> bool:
        """Atualizar dados do tenant"""
        try:
            self.db.table('tenants').update(dados).eq('id', tenant_id).execute()
            return True
        except Exception as e:
            print(f"Erro ao atualizar tenant: {e}")
            return False
    
    # ─────────────────────────────────────────────────────────────────────────
    # CLIENT OPERATIONS
    # ─────────────────────────────────────────────────────────────────────────
    
    def criar_cliente(self, tenant_id: str, dados: Dict) -> Optional[Dict]:
        """
        Criar novo cliente
        
        Args:
            tenant_id: ID do tenant
            dados: Dados do cliente (nome, email, telefone, etc)
        
        Returns:
            Dados do cliente criado
        """
        dados['tenant_id'] = tenant_id
        try:
            response = self.db.table('clients').insert(dados).execute()
            return response.data[0] if response.data else None
        except Exception as e:
            print(f"Erro ao criar cliente: {e}")
            return None
    
    def listar_clientes(self, tenant_id: str, grupo: Optional[str] = None) -> List[Dict]:
        """
        Listar clientes de um tenant
        
        Args:
            tenant_id: ID do tenant
            grupo: Filtrar por grupo (VIP, Normal, etc)
        
        Returns:
            Lista de clientes
        """
        try:
            query = self.db.table('clients').eq('tenant_id', tenant_id)
            if grupo:
                query = query.eq('grupo_cliente', grupo)
            response = query.select('*').execute()
            return response.data
        except Exception as e:
            print(f"Erro ao listar clientes: {e}")
            return []
    
    # ─────────────────────────────────────────────────────────────────────────
    # PRODUCT OPERATIONS
    # ─────────────────────────────────────────────────────────────────────────
    
    def criar_produto(self, tenant_id: str, dados: Dict) -> Optional[Dict]:
        """Criar novo produto"""
        dados['tenant_id'] = tenant_id
        try:
            response = self.db.table('products').insert(dados).execute()
            return response.data[0] if response.data else None
        except Exception as e:
            print(f"Erro ao criar produto: {e}")
            return None
    
    def atualizar_estoque(self, produto_id: str, nova_quantidade: float) -> bool:
        """Atualizar quantidade em estoque"""
        try:
            self.db.table('products')\
                .update({'estoque': nova_quantidade})\
                .eq('id', produto_id)\
                .execute()
            return True
        except Exception as e:
            print(f"Erro ao atualizar estoque: {e}")
            return False
    
    def movimentar_estoque(self, tenant_id: str, dados: Dict) -> Optional[Dict]:
        """
        Registrar movimentação de estoque
        
        Args:
            tenant_id: ID do tenant
            dados: {
                'produto_id': UUID,
                'tipo': 'entrada|saida|ajuste|devolucao',
                'quantidade': 10,
                'motivo': 'Venda PDV',
                'referencia_tipo': 'venda',
                'referencia_id': UUID
            }
        
        Returns:
            Dados da movimentação criada
        """
        dados['tenant_id'] = tenant_id
        try:
            response = self.db.table('estoque_movimentacoes').insert(dados).execute()
            return response.data[0] if response.data else None
        except Exception as e:
            print(f"Erro ao movimentar estoque: {e}")
            return None
    
    def produtos_estoque_baixo(self, tenant_id: str) -> List[Dict]:
        """Listar produtos com estoque abaixo do mínimo"""
        try:
            response = self.db.table('produtos_estoque_baixo')\
                .eq('tenant_id', tenant_id)\
                .select('*')\
                .execute()
            return response.data
        except Exception as e:
            print(f"Erro ao listar estoque baixo: {e}")
            return []
    
    # ─────────────────────────────────────────────────────────────────────────
    # FINANCIAL OPERATIONS
    # ─────────────────────────────────────────────────────────────────────────
    
    def registrar_transacao(self, tenant_id: str, dados: Dict) -> Optional[Dict]:
        """
        Registrar transação financeira
        
        Args:
            tenant_id: ID do tenant
            dados: {
                'tipo': 'receita|despesa|transferencia',
                'categoria': 'venda',
                'descricao': 'Venda PDV #123',
                'valor': 150.50,
                'data_vencimento': '2024-01-15',
                'pago': False
            }
        
        Returns:
            Dados da transação criada
        """
        dados['tenant_id'] = tenant_id
        try:
            response = self.db.table('transactions').insert(dados).execute()
            return response.data[0] if response.data else None
        except Exception as e:
            print(f"Erro ao registrar transação: {e}")
            return None
    
    def marcar_pago(self, transacao_id: str) -> bool:
        """Marcar transação como paga"""
        try:
            self.db.table('transactions')\
                .update({
                    'pago': True,
                    'data_pagamento': datetime.now().isoformat()
                })\
                .eq('id', transacao_id)\
                .execute()
            return True
        except Exception as e:
            print(f"Erro ao marcar transação como paga: {e}")
            return False
    
    def relatorio_financeiro(self, tenant_id: str, 
                           data_inicio: str, data_fim: str) -> Dict:
        """
        Gerar relatório financeiro
        
        Args:
            tenant_id: ID do tenant
            data_inicio: YYYY-MM-DD
            data_fim: YYYY-MM-DD
        
        Returns:
            {
                'receitas': 1000.00,
                'despesas': 500.00,
                'liquido': 500.00,
                'pendente': 250.00
            }
        """
        try:
            # Receitas
            receitas_resp = self.db.table('transactions')\
                .eq('tenant_id', tenant_id)\
                .eq('tipo', 'receita')\
                .gte('data_criacao', f"{data_inicio}T00:00:00")\
                .lte('data_criacao', f"{data_fim}T23:59:59")\
                .select('valor')\
                .execute()
            
            receitas = sum(t['valor'] for t in receitas_resp.data) if receitas_resp.data else 0
            
            # Despesas
            despesas_resp = self.db.table('transactions')\
                .eq('tenant_id', tenant_id)\
                .eq('tipo', 'despesa')\
                .gte('data_criacao', f"{data_inicio}T00:00:00")\
                .lte('data_criacao', f"{data_fim}T23:59:59")\
                .select('valor')\
                .execute()
            
            despesas = sum(t['valor'] for t in despesas_resp.data) if despesas_resp.data else 0
            
            # Pendentes
            pendentes_resp = self.db.table('transactions')\
                .eq('tenant_id', tenant_id)\
                .eq('pago', False)\
                .lte('data_vencimento', data_fim)\
                .select('valor')\
                .execute()
            
            pendentes = sum(t['valor'] for t in pendentes_resp.data) if pendentes_resp.data else 0
            
            return {
                'receitas': receitas,
                'despesas': despesas,
                'liquido': receitas - despesas,
                'pendente': pendentes
            }
        
        except Exception as e:
            print(f"Erro ao gerar relatório financeiro: {e}")
            return {'receitas': 0, 'despesas': 0, 'liquido': 0, 'pendente': 0}
    
    def clientes_inadimplentes(self, tenant_id: str) -> List[Dict]:
        """Listar clientes com contas vencidas"""
        try:
            response = self.db.table('clientes_inadimplentes')\
                .eq('tenant_id', tenant_id)\
                .select('*')\
                .execute()
            return response.data
        except Exception as e:
            print(f"Erro ao listar inadimplentes: {e}")
            return []
    
    # ─────────────────────────────────────────────────────────────────────────
    # ANALYTICS
    # ─────────────────────────────────────────────────────────────────────────
    
    def dashboard_metrics(self, tenant_id: str) -> Dict:
        """Obter métricas para dashboard"""
        try:
            # Total de clientes
            clients = self.db.table('clients')\
                .eq('tenant_id', tenant_id)\
                .eq('ativo', True)\
                .select('id')\
                .execute()
            
            # Total de produtos
            products = self.db.table('products')\
                .eq('tenant_id', tenant_id)\
                .eq('ativo', True)\
                .select('id')\
                .execute()
            
            # Receita hoje
            hoje = datetime.now().strftime('%Y-%m-%d')
            receita_resp = self.db.table('transactions')\
                .eq('tenant_id', tenant_id)\
                .eq('tipo', 'receita')\
                .gte('data_criacao', f"{hoje}T00:00:00")\
                .lte('data_criacao', f"{hoje}T23:59:59")\
                .select('valor')\
                .execute()
            
            receita_hoje = sum(t['valor'] for t in receita_resp.data) if receita_resp.data else 0
            
            return {
                'total_clientes': len(clients.data) if clients.data else 0,
                'total_produtos': len(products.data) if products.data else 0,
                'receita_hoje': receita_hoje
            }
        
        except Exception as e:
            print(f"Erro ao obter métricas: {e}")
            return {
                'total_clientes': 0,
                'total_produtos': 0,
                'receita_hoje': 0
            }


# ═══════════════════════════════════════════════════════════════════════════
# EXEMPLO DE USO
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    db = DatabaseManager()
    
    # Criar tenant
    tenant = db.criar_tenant({
        'slug': 'restaurante-paulista',
        'nome': 'Restaurante Paulista',
        'tipo': 'restaurante',
        'cnpj': '10.123.456/0001-78',
        'email': 'contato@restpaulista.com.br'
    })
    
    if tenant:
        print(f"✓ Tenant criado: {tenant['id']}")
        
        # Criar cliente
        cliente = db.criar_cliente(tenant['id'], {
            'nome': 'João Silva',
            'email': 'joao@email.com',
            'telefone': '11999999999',
            'grupo_cliente': 'VIP'
        })
        
        if cliente:
            print(f"✓ Cliente criado: {cliente['id']}")
        
        # Métricas
        metricas = db.dashboard_metrics(tenant['id'])
        print(f"✓ Métricas: {metricas}")
    else:
        print("✗ Erro ao criar tenant")
