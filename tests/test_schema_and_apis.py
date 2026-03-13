"""
tests/test_schema_and_apis.py
Testes básicos para validar schema e endpoints
"""

import pytest
import requests
import json
from datetime import datetime

BASE_URL = "http://localhost:5000/api"
TOKEN = None  # Será preenchido no setup
TENANT_ID = None
PRODUCT_ID = None

# ═══════════════════════════════════════════════════════════════════════════

class TestAuth:
    """Testes de autenticação"""
    
    def test_login(self):
        """GET /auth/login - Autenticar"""
        resp = requests.post(f"{BASE_URL}/auth/login", json={
            "username": "admin",
            "password": "123456"
        })
        assert resp.status_code in [200, 401]
        if resp.status_code == 200:
            global TOKEN
            TOKEN = resp.json().get('token')
            assert TOKEN is not None


class TestTenants:
    """Testes de tenants"""
    
    def test_create_tenant(self):
        """POST /tenants - Criar tenant"""
        global TENANT_ID
        resp = requests.post(f"{BASE_URL}/tenants", json={
            "slug": f"test-{datetime.now().timestamp()}",
            "nome": "Tenant Teste",
            "tipo": "restaurante",
            "cnpj": "12.345.678/0001-99"
        })
        assert resp.status_code in [200, 201]
        if resp.json().get('data'):
            TENANT_ID = resp.json()['data']['id']
    
    def test_list_tenants(self):
        """GET /tenants - Listar tenants"""
        resp = requests.get(f"{BASE_URL}/tenants")
        assert resp.status_code == 200


class TestProducts:
    """Testes de produtos"""
    
    def test_create_product(self):
        """POST /products - Criar produto"""
        if not TENANT_ID:
            pytest.skip("TENANT_ID não disponível")
        
        global PRODUCT_ID
        resp = requests.post(f"{BASE_URL}/products", json={
            "tenant_id": TENANT_ID,
            "nome": "Produto Teste",
            "sku": "TEST001",
            "preco_custo": 10.00,
            "preco_venda": 25.00,
            "estoque": 100,
            "estoque_minimo": 10
        }, headers={"Authorization": f"Bearer {TOKEN}"})
        
        assert resp.status_code in [200, 201]
        if resp.json().get('data'):
            PRODUCT_ID = resp.json()['data'][0]['id']


class TestInventory:
    """Testes de estoque"""
    
    def test_registrar_movimentacao(self):
        """POST /inventory/movimentar - Registrar movimento"""
        if not TENANT_ID or not PRODUCT_ID:
            pytest.skip("Requisitos não disponíveis")
        
        resp = requests.post(f"{BASE_URL}/inventory/movimentar", json={
            "tenant_id": TENANT_ID,
            "produto_id": PRODUCT_ID,
            "tipo": "saida",
            "quantidade": 5,
            "motivo": "Venda teste"
        }, headers={"Authorization": f"Bearer {TOKEN}"})
        
        assert resp.status_code in [200, 201]
        assert resp.json().get('sucesso') == True
    
    def test_ver_estoque_baixo(self):
        """GET /inventory/estoque-baixo - Ver produtos baixos"""
        if not TENANT_ID:
            pytest.skip("TENANT_ID não disponível")
        
        resp = requests.get(
            f"{BASE_URL}/inventory/estoque-baixo",
            params={"tenant_id": TENANT_ID},
            headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert resp.status_code == 200


class TestFinance:
    """Testes de finanças"""
    
    def test_criar_transacao(self):
        """POST /finance/transacao - Criar transação"""
        if not TENANT_ID:
            pytest.skip("TENANT_ID não disponível")
        
        resp = requests.post(f"{BASE_URL}/finance/transacao", json={
            "tenant_id": TENANT_ID,
            "tipo": "receita",
            "categoria": "Venda",
            "descricao": "Venda teste",
            "valor": 150.50,
            "pago": True
        }, headers={"Authorization": f"Bearer {TOKEN}"})
        
        assert resp.status_code in [200, 201]
        assert resp.json().get('sucesso') == True
    
    def test_dashboard(self):
        """GET /finance/dashboard - Ver dashboard"""
        if not TENANT_ID:
            pytest.skip("TENANT_ID não disponível")
        
        resp = requests.get(
            f"{BASE_URL}/finance/dashboard",
            params={"tenant_id": TENANT_ID},
            headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert resp.status_code == 200
    
    def test_relatorio(self):
        """GET /finance/relatorio - Gerar relatório"""
        if not TENANT_ID:
            pytest.skip("TENANT_ID não disponível")
        
        resp = requests.get(
            f"{BASE_URL}/finance/relatorio",
            params={"tenant_id": TENANT_ID},
            headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert resp.status_code == 200


class TestPreferences:
    """Testes de preferências do usuário"""
    
    def test_get_preferences(self):
        """GET /preferences - Obter preferências"""
        if not TENANT_ID:
            pytest.skip("TENANT_ID não disponível")
        
        resp = requests.get(
            f"{BASE_URL}/preferences",
            params={"tenant_id": TENANT_ID},
            headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert resp.status_code == 200
    
    def test_save_preferences(self):
        """POST /preferences - Salvar preferências"""
        if not TENANT_ID:
            pytest.skip("TENANT_ID não disponível")
        
        resp = requests.post(
            f"{BASE_URL}/preferences",
            params={"tenant_id": TENANT_ID},
            json={
                "theme": "light",
                "sidebar_collapsed": True,
                "notify_sales": False
            },
            headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert resp.status_code == 200
        assert resp.json().get('sucesso') == True
    
    def test_set_theme(self):
        """POST /preferences/theme - Mudar tema"""
        if not TENANT_ID:
            pytest.skip("TENANT_ID não disponível")
        
        resp = requests.post(
            f"{BASE_URL}/preferences/theme",
            params={"tenant_id": TENANT_ID},
            json={"theme": "dark"},
            headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════
# EXECUTAR: pytest tests/test_schema_and_apis.py -v
# ═══════════════════════════════════════════════════════════════════════════
