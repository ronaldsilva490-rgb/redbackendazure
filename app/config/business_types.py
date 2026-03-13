"""
TIPOS DE NEGÓCIOS - Configuração de Módulos
RED COMMERCIAL v5.0

Cada tipo de negócio tem módulos específicos e campos personalizados.
"""

BUSINESS_TYPES = {
    "restaurante": {
        "nome": "Restaurante",
        "icone": "🍽️",
        "descricao": "Restaurante, lanchonete, cafeteria",
        "modulos": [
            "dashboard",
            "menu_cardapio",
            "mesas",
            "cozinha",
            "garcom",
            "pedidos",
            "caixa",
            "estoque",
            "clientes",
            "relatorios",
            "entregas",
            "alergenios"
        ],
        "funcionalidades": [
            "mesa_acompanhamento",
            "split_conta",
            "impressora_cozinha",
            "chamada_garcom",
            "delivery_integrado",
            "cupom_fiscal",
            "comanda_aberta"
        ],
        "campos_customizados": [
            "horario_funcionamento",
            "endereco_entrega",
            "taxa_entrega",
            "tempo_preparo_estimado"
        ]
    },

    "concessionaria": {
        "nome": "Concessionária",
        "icone": "🚗",
        "descricao": "Venda de veículos, oficina",
        "modulos": [
            "dashboard",
            "inventario_veiculos",
            "vendas_veiculos",
            "workshop_oficina",
            "clientes",
            "financeiro",
            "manutencao",
            "estoque_pecas",
            "relatorios",
            "documentos"
        ],
        "funcionalidades": [
            "tabela_preco_dinamica",
            "juros_parcelado",
            "ficha_veiculo",
            "pdf_contrato",
            "agendamento_manutencao",
            "rastreamento_obra",
            "teste_drive"
        ],
        "campos_customizados": [
            "renavan",
            "placa",
            "motor",
            "chassi",
            "cor",
            "ano_fabricacao",
            "ano_modelo"
        ]
    },

    "comercio": {
        "nome": "Comércio Varejista",
        "icone": "🏪",
        "descricao": "Loja, boutique, varejo geral",
        "modulos": [
            "dashboard",
            "catalogo_produtos",
            "vendas_pdv",
            "estoque",
            "clientes",
            "financeiro",
            "relatorios",
            "promocoes",
            "fornecedores"
        ],
        "funcionalidades": [
            "codigo_barras",
            "preco_dinamico",
            "programa_fidelidade",
            "cupons_desconto",
            "promotroca",
            "integracao_api",
            "multiplos_PDV"
        ],
        "campos_customizados": [
            "sku",
            "codigo_barras",
            "tamanho",
            "cor",
            "categoria",
            "estoque_minimo"
        ]
    },

    "farmacia": {
        "nome": "Farmácia",
        "icone": "💊",
        "descricao": "Farmácia, drogaria",
        "modulos": [
            "dashboard",
            "catalogo_medicamentos",
            "vendas",
            "estoque",
            "receitas",
            "controle_tarja",
            "cliente_programa_fidelidade",
            "financeiro",
            "relatorios",
            "conformidade_anvisa"
        ],
        "funcionalidades": [
            "validacao_receita",
            "validade_medicamento",
            "tarja_vermelha_controle",
            "generico_similar_generico",
            "integracao_seguro_saude",
            "historico_paciente",
            "alertas_interacoes"
        ],
        "campos_customizados": [
            "principio_ativo",
            "laboratorio",
            "data_validade",
            "lote",
            "tarja",
            "necessita_receita",
            "controlado"
        ]
    },

    "clinica_consultorio": {
        "nome": "Clínica / Consultório",
        "icone": "🏥",
        "descricao": "Consultório médico, clínica, odontologia",
        "modulos": [
            "dashboard",
            "pacientes",
            "agendamentos",
            "consultas",
            "prescricoes",
            "cobranc a",
            "caixa",
            "estoque_medicamentos",
            "relatorios",
            "prontuario_eletronico"
        ],
        "funcionalidades": [
            "agenda_medicos",
            "ficha_paciente",
            "prontuario_eletronico",
            "receituario",
            "atestado",
            "integracao_laboratorio",
            "lembretes_retorno",
            "historico_atendimento"
        ],
        "campos_customizados": [
            "crm_medico",
            "especialidade",
            "convenios_aceitos",
            "horario_atendimento",
            "tempo_consulta"
        ]
    },

    "salao_beleza": {
        "nome": "Salão de Beleza",
        "icone": "💇",
        "descricao": "Salão de beleza, cabeleireiro, spa",
        "modulos": [
            "dashboard",
            "agenda_profissionais",
            "servicos",
            "clientes",
            "vendas_produtos",
            "caixa",
            "estoque_produtos",
            "comissoes",
            "relatorios",
            "programa_fidelidade"
        ],
        "funcionalidades": [
            "agenda_online",
            "confirmacao_automatica",
            "avaliacoes_servicos",
            "combo_servicos",
            "calculo_comissoes",
            "programa_fidelidade",
            "lembrete_manutencao"
        ],
        "campos_customizados": [
            "profissional",
            "duracao_servico",
            "preco_servico",
            "comissao_percentual",
            "produtos_utilizados"
        ]
    },

    "academia": {
        "nome": "Academia / Gym",
        "icone": "💪",
        "descricao": "Academia, personal trainer, crossfit",
        "modulos": [
            "dashboard",
            "alunos",
            "planos_mensalidades",
            "caixa",
            "aulas_agendadas",
            "personal_trainers",
            "estoque_suplementos",
            "vendas",
            "relatorios",
            "comunicados"
        ],
        "funcionalidades": [
            "cartao_acesso",
            "registro_frequencia",
            "planos_customizados",
            "cobranca_automatica",
            "boletos",
            "acompanhamento_progresso",
            "avisos_vencimento",
            "vendas_suplementos"
        ],
        "campos_customizados": [
            "plano_tipo",
            "duracao_meses",
            "valor_mensal",
            "data_vencimento",
            "modalidades_acesso"
        ]
    },

    "hotel_hospedagem": {
        "nome": "Hotel / Hospedagem",
        "icone": "🏨",
        "descricao": "Hotel, pousada, resort",
        "modulos": [
            "dashboard",
            "quartos_hospedes",
            "reservas",
            "check_in_check_out",
            "servicos_adicionais",
            "restaurante",
            "caixa",
            "limpeza_manutencao",
            "relatorios",
            "avaliacoes"
        ],
        "funcionalidades": [
            "sistema_reservas",
            "disponibilidade_quartos",
            "preco_dinamico",
            "servico_quarto",
            "avaliacao_hospedes",
            "historico_hospede",
            "checkout_automatico"
        ],
        "campos_customizados": [
            "numero_quarto",
            "tipo_quarto",
            "capacidade",
            "preco_noite",
            "amenidades",
            "data_checkin",
            "data_checkout"
        ]
    },

    "padaria_confeitaria": {
        "nome": "Padaria / Confeitaria",
        "icone": "🥐",
        "descricao": "Padaria, confeitaria, doces",
        "modulos": [
            "dashboard",
            "receitas_producao",
            "ingredientes_estoque",
            "producao_diaria",
            "vendas_pdv",
            "encomendas",
            "clientes",
            "caixa",
            "entrega",
            "relatorios"
        ],
        "funcionalidades": [
            "controle_receita",
            "calculo_custo",
            "agenda_encomendas",
            "producao_agendada",
            "validade_ingredientes",
            "encomenda_personalizada",
            "agendamento_entrega"
        ],
        "campos_customizados": [
            "ingrediente_quantidade",
            "data_producao",
            "data_validade",
            "receita_formula",
            "valor_custo",
            "valor_venda"
        ]
    },

    "ecommerce": {
        "nome": "Loja Online",
        "icone": "🛍️",
        "descricao": "E-commerce, marketplace",
        "modulos": [
            "dashboard",
            "produtos_catalogo",
            "carrinho_compras",
            "pedidos",
            "pagamentos",
            "frete_logistica",
            "clientes",
            "marketing_email",
            "relatorios",
            "integracao_api"
        ],
        "funcionalidades": [
            "catalogo_dinamico",
            "preco_variacoes",
            "carrinho_persistente",
            "gateway_pagamento",
            "calculo_frete",
            "rastreamento_pedido",
            "programa_afiliados",
            "cupons_desconto"
        ],
        "campos_customizados": [
            "sku",
            "variacao_tamanho",
            "variacao_cor",
            "peso_frete",
            "dimensoes",
            "imagens_produto"
        ]
    },

    "prestador_servicos": {
        "nome": "Prestador de Serviços",
        "icone": "🔧",
        "descricao": "Encanador, eletricista, marido de aluguel, etc",
        "modulos": [
            "dashboard",
            "servicos",
            "clientes",
            "agenda_servicos",
            "visitas",
            "orcamentos",
            "cobranc a",
            "caixa",
            "estoque_materiais",
            "relatorios"
        ],
        "funcionalidades": [
            "sistema_orcamento",
            "agendamento_visita",
            "rastreamento_rota",
            "assinatura_digital",
            "foto_antes_depois",
            "avaliacao_cliente",
            "remessa_nota_fiscal"
        ],
        "campos_customizados": [
            "endereco_atendimento",
            "tipo_servico",
            "materiais_inclusos",
            "valor_mao_obra",
            "valor_materiais",
            "tempo_estimado"
        ]
    },

    "supermercado": {
        "nome": "Supermercado",
        "icone": "🛒",
        "descricao": "Supermercado, hipermercado",
        "modulos": [
            "dashboard",
            "catalogo_produtos",
            "caixas_pdv_multiplos",
            "estoque_deposito",
            "fornecedores",
            "promocoes_ofertas",
            "clientes_programa",
            "relatorios",
            "nfe_emissao",
            "auditoria_caixa"
        ],
        "funcionalidades": [
            "codigo_barras",
            "multiplos_caixas",
            "preco_promocional_temporal",
            "estoque_real_time",
            "reposicao_automatica",
            "cupom_fiscal_eletr",
            "programa_fidelidade",
            "controle_perdas"
        ],
        "campos_customizados": [
            "prateleira_localizacao",
            "validade_data",
            "lote",
            "preco_custo",
            "margem_lucro",
            "fornecedor_principal"
        ]
    },

    "distribuidora": {
        "nome": "Distribuidora",
        "icone": "📦",
        "descricao": "Distribuidora, venda B2B",
        "modulos": [
            "dashboard",
            "produtos_catalogo",
            "clientes_revenda",
            "pedidos_venda",
            "estoque",
            "logistica_entrega",
            "financeiro_credito",
            "relatorios",
            "integracao_nfe"
        ],
        "funcionalidades": [
            "tabela_preco_cliente",
            "credito_disponivel",
            "condicoes_pagamento",
            "roteiriza_entrega",
            "rastreamento_veiculo",
            "nota_fiscal_eletronica",
            "integracao_portal_cliente"
        ],
        "campos_customizados": [
            "cliente_cnpj",
            "creditolimite",
            "condicao_pagamento",
            "rota_entrega",
            "vendedor_responsavel",
            "tabela_preco_especial"
        ]
    }
}

# ═══════════════════════════════════════════════════════════════════════════════

def get_business_type(tipo):
    """Retorna config de um tipo de negócio"""
    return BUSINESS_TYPES.get(tipo)

def get_all_business_types():
    """Retorna lista de todos os tipos disponíveis"""
    return list(BUSINESS_TYPES.keys())

def get_business_modules(tipo):
    """Retorna módulos de um tipo de negócio"""
    config = BUSINESS_TYPES.get(tipo)
    return config['modulos'] if config else []

def get_business_features(tipo):
    """Retorna funcionalidades de um tipo de negócio"""
    config = BUSINESS_TYPES.get(tipo)
    return config['funcionalidades'] if config else []

def validate_business_type(tipo):
    """Valida se tipo é válido"""
    return tipo in BUSINESS_TYPES
