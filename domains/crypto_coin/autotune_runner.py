"""
Auto-Tuner — re-otimiza os parâmetros automaticamente
=========================================================
Pode ser chamado de duas formas:

  1. Manual (linha de comando):
       python autotune.py

  2. Automático (integrado ao bot — roda toda semana em background)

O que ele faz:
  - Baixa dados históricos recentes da exchange
  - Roda o algoritmo genético para encontrar os melhores parâmetros
  - Valida os parâmetros em dados out-of-sample (evita overfitting)
  - Atualiza o .env com os novos parâmetros SE forem melhores que os atuais
  - Notifica via Telegram com o resultado
"""

import argparse
import asyncio
import copy
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import ccxt.async_support as ccxt
import pandas as pd

from domains.crypto_coin.config.settings import load_config, Config
from domains.crypto_coin.autotune.optimizer import GeneticOptimizer, Individual, evaluate
from domains.crypto_coin.infra.notifier import Notifier


# ── Busca de dados históricos ─────────────────────────────────────────────────

async def fetch_ohlcv(symbol: str, timeframe: str, days: int, exchange_id: str) -> pd.DataFrame:
    cls = getattr(ccxt, exchange_id)
    ex = cls({"enableRateLimit": True})
    since_ms = int((datetime.utcnow().timestamp() - days * 86400) * 1000)
    candles = []
    try:
        while True:
            batch = await ex.fetch_ohlcv(symbol, timeframe, since=since_ms, limit=1000)
            if not batch:
                break
            candles.extend(batch)
            since_ms = batch[-1][0] + 1
            if len(batch) < 1000:
                break
            await asyncio.sleep(0.3)
    finally:
        await ex.close()

    df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df.set_index("timestamp")


# ── Validação out-of-sample ───────────────────────────────────────────────────

def validate_out_of_sample(best: Individual, df_val: pd.DataFrame) -> dict:
    """
    Testa o melhor indivíduo em dados que NÃO foram usados no treino.
    Evita que o bot seja otimizado para o passado e falhe no presente.
    """
    val = copy.deepcopy(best)
    # Reseta métricas para não herdar valores do treino se não houver trades
    val.total_return_pct = 0.0
    val.win_rate = 0.0
    val.total_trades = 0
    val.sharpe = 0.0
    val = evaluate(val, df_val)
    return {
        "return_pct": val.total_return_pct if val.total_trades >= 1 else 0.0,
        "win_rate": val.win_rate,
        "trades": val.total_trades,
        "fitness": val.fitness,
        "sharpe": val.sharpe,
    }


# ── Atualização do .env ───────────────────────────────────────────────────────

def update_env(best: Individual, env_path: str = ".env"):
    """
    Atualiza os parâmetros no arquivo .env preservando todas as outras configs
    (API keys, exchange, etc.).
    """
    if not Path(env_path).exists():
        raise FileNotFoundError(f"Arquivo {env_path} não encontrado.")

    mapping = {
        "MA_FAST":          str(best.ma_fast),
        "MA_SLOW":          str(best.ma_slow),
        "RSI_PERIOD":       str(best.rsi_period),
        "RSI_OVERSOLD":     str(int(best.rsi_oversold)),
        "RSI_OVERBOUGHT":   str(int(best.rsi_overbought)),
        "BB_PERIOD":        str(best.bb_period),
        "BB_STD":           str(round(best.bb_std, 1)),
        "STOP_LOSS_PCT":    str(round(best.stop_loss_pct, 1)),
        "TAKE_PROFIT_PCT":  str(round(best.take_profit_pct, 1)),
        "TRADE_SIZE_PCT":   str(int(best.trade_size_pct)),
    }

    with open(env_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    updated_keys = set()
    new_lines = []
    for line in lines:
        matched = False
        for key, val in mapping.items():
            pattern = rf"^{key}\s*="
            if re.match(pattern, line.strip()):
                new_lines.append(f"{key}={val}\n")
                updated_keys.add(key)
                matched = True
                break
        if not matched:
            new_lines.append(line)

    # Adiciona chaves que não existiam
    for key, val in mapping.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={val}\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


def save_optimization_result(best: Individual, val_result: dict, cfg: Config):
    """Salva histórico de otimizações para auditoria."""
    Path("logs").mkdir(exist_ok=True)
    record = {
        "timestamp": datetime.utcnow().isoformat(),
        "symbol": cfg.symbol,
        "timeframe": cfg.timeframe,
        "params": {
            "ma_fast": best.ma_fast,
            "ma_slow": best.ma_slow,
            "rsi_period": best.rsi_period,
            "rsi_oversold": best.rsi_oversold,
            "rsi_overbought": best.rsi_overbought,
            "bb_period": best.bb_period,
            "bb_std": best.bb_std,
            "stop_loss_pct": best.stop_loss_pct,
            "take_profit_pct": best.take_profit_pct,
            "trade_size_pct": best.trade_size_pct,
        },
        "train": {
            "return_pct": best.total_return_pct,
            "win_rate": best.win_rate,
            "trades": best.total_trades,
            "sharpe": best.sharpe,
            "fitness": best.fitness,
        },
        "validation": val_result,
    }
    with open("logs/autotune_history.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# ── Runner principal ──────────────────────────────────────────────────────────

class AutoTuner:
    def __init__(
        self,
        cfg: Config,
        logger: logging.Logger,
        train_days: int = 60,
        val_days: int = 14,
        population: int = 40,
        generations: int = 25,
        min_val_return: float = 0.0,   # Aceita novos params só se retorno val > X%
    ):
        self.cfg = cfg
        self.logger = logger
        self.train_days = train_days
        self.val_days = val_days
        self.population = population
        self.generations = generations
        self.min_val_return = min_val_return
        self.notifier = Notifier(cfg, logger)

    async def run(self, update_env_file: bool = True) -> Optional[Individual]:
        total_days = self.train_days + self.val_days
        self.logger.info("=" * 60)
        self.logger.info("  🔬 AUTO-TUNER INICIADO")
        self.logger.info(f"  Par: {self.cfg.symbol} | Timeframe: {self.cfg.timeframe}")
        self.logger.info(f"  Treino: {self.train_days}d | Validação: {self.val_days}d")
        self.logger.info("=" * 60)

        await self.notifier.send(
            f"🔬 Auto-tuner iniciado\n"
            f"Par: {self.cfg.symbol} | {self.cfg.timeframe}\n"
            f"Baixando {total_days} dias de dados..."
        )

        # 1. Baixar dados
        self.logger.info(f"📥 Baixando {total_days} dias de dados históricos...")
        try:
            df = await fetch_ohlcv(
                self.cfg.symbol, self.cfg.timeframe, total_days, self.cfg.exchange
            )
        except Exception as e:
            self.logger.error(f"Falha ao baixar dados: {e}")
            return None

        self.logger.info(f"   {len(df)} candles carregados ({df.index[0].date()} → {df.index[-1].date()})")

        # 2. Dividir treino / validação (walk-forward)
        split_idx = int(len(df) * (self.train_days / total_days))
        df_train = df.iloc[:split_idx]
        df_val = df.iloc[split_idx:]

        self.logger.info(f"   Treino: {len(df_train)} candles | Validação: {len(df_val)} candles")

        # 3. Otimização genética no conjunto de treino
        optimizer = GeneticOptimizer(
            df=df_train,
            population_size=self.population,
            generations=self.generations,
            logger=self.logger,
        )
        best = optimizer.run()
        optimizer.save_history()

        # 4. Validação out-of-sample
        self.logger.info("\n📋 Validando em dados out-of-sample...")
        val_result = validate_out_of_sample(best, df_val)
        self.logger.info(
            f"   Retorno val:  {val_result['return_pct']:+.2f}%\n"
            f"   Win rate val: {val_result['win_rate']:.1f}%\n"
            f"   Trades val:   {val_result['trades']}\n"
            f"   Sharpe val:   {val_result['sharpe']:.3f}"
        )

        # 5. Decidir se aplica os novos parâmetros
        accepted = val_result["return_pct"] >= self.min_val_return and val_result["trades"] >= 2

        if accepted and update_env_file:
            self.logger.info("✅ Parâmetros aceitos — atualizando .env...")
            update_env(best)
            self.logger.info("   .env atualizado com sucesso.")
        elif not accepted:
            self.logger.warning(
                f"⚠️  Parâmetros REJEITADOS — retorno de validação ({val_result['return_pct']:+.2f}%) "
                f"abaixo do mínimo ({self.min_val_return}%). Mantendo parâmetros atuais."
            )

        # 6. Salvar histórico
        save_optimization_result(best, val_result, self.cfg)

        # 7. Notificação
        status = "✅ Aplicados" if accepted else "⚠️ Rejeitados"
        msg = (
            f"🧬 Auto-tuner concluído — {status}\n\n"
            f"Treino:     {best.total_return_pct:+.2f}% | WR {best.win_rate:.0f}%\n"
            f"Validação:  {val_result['return_pct']:+.2f}% | WR {val_result['win_rate']:.0f}%\n\n"
            f"Parâmetros:\n"
            f"  MA: {best.ma_fast}/{best.ma_slow}\n"
            f"  RSI: {best.rsi_period}p ({best.rsi_oversold}/{best.rsi_overbought})\n"
            f"  SL: {best.stop_loss_pct}% | TP: {best.take_profit_pct}%\n"
            f"  Trade size: {best.trade_size_pct}%"
        )
        await self.notifier.send(msg)
        self.logger.info(msg)

        return best if accepted else None


# ── Execução via linha de comando ─────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="CryptoBot Auto-Tuner")
    parser.add_argument("--train-days",   type=int,   default=60,   help="Dias de treino (padrão: 60)")
    parser.add_argument("--val-days",     type=int,   default=14,   help="Dias de validação (padrão: 14)")
    parser.add_argument("--population",   type=int,   default=40,   help="Tamanho da população genética")
    parser.add_argument("--generations",  type=int,   default=25,   help="Número de gerações")
    parser.add_argument("--min-val",      type=float, default=0.0,  help="Retorno minimo na validacao para aceitar (pct)")
    parser.add_argument("--dry-run",      action="store_true",      help="Não atualiza o .env, só mostra resultado")
    args = parser.parse_args()

    from domains.crypto_coin.infra.logger import setup_logger
    logger = setup_logger()

    os.environ.setdefault("PAPER_TRADING", "true")
    cfg = load_config()

    tuner = AutoTuner(
        cfg=cfg,
        logger=logger,
        train_days=args.train_days,
        val_days=args.val_days,
        population=args.population,
        generations=args.generations,
        min_val_return=args.min_val,
    )

    best = await tuner.run(update_env_file=not args.dry_run)

    if best:
        print("\n✅ Otimização concluída. Parâmetros atualizados no .env.")
        print("   Reinicie o bot para aplicar as mudanças: python bot.py")
    else:
        print("\n⚠️  Parâmetros rejeitados ou otimização falhou. .env não foi alterado.")


if __name__ == "__main__":
    asyncio.run(main())
