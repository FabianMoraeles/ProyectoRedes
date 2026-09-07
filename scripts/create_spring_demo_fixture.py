"""Write the tiny synthetic Spring Boot project used to demo `spring_architecture`.

Why this exists
----------------
Diego Lopez's ``spring-architecture-analyzer-mcp`` (classmate_server_2) needs a
real Maven Spring Boot repository to analyze; this project ships no such codebase.
Rather than pointing it at an unrelated cloned project (not reproducible, not
explainable line by line), this script writes a minimal one with two properties
planted on purpose, so the tools have something real to detect:

* a dependency cycle: ``OrderService -> PricingService -> OrderService``;
* a layering violation: ``OrderController`` depends on ``OrderRepository``
  directly, skipping the service layer.

Usage::

    uv run python scripts/create_spring_demo_fixture.py

Idempotent: re-running it overwrites the same five files with the same content.
See ``config/demo-prompts.md`` section 6 for the scenario that analyzes it.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path("demo_workspace/spring-demo")
PACKAGE_DIR = ROOT / "src/main/java/com/example/order"

FILES = {
    "pom.xml": """\
<project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <groupId>com.example</groupId>
    <artifactId>spring-demo</artifactId>
    <version>0.0.1-SNAPSHOT</version>
    <packaging>jar</packaging>
</project>
""",
    "OrderController.java": """\
package com.example.order;

import org.springframework.web.bind.annotation.RestController;

@RestController
public class OrderController {
    private final OrderService orderService;
    private final OrderRepository orderRepository;

    public OrderController(OrderService orderService, OrderRepository orderRepository) {
        this.orderService = orderService;
        // Deliberate layer violation: a Controller talking directly to a Repository.
        this.orderRepository = orderRepository;
    }
}
""",
    "OrderService.java": """\
package com.example.order;

import org.springframework.stereotype.Service;

@Service
public class OrderService {
    private final OrderRepository orderRepository;
    private final PricingService pricingService;

    public OrderService(OrderRepository orderRepository, PricingService pricingService) {
        this.orderRepository = orderRepository;
        this.pricingService = pricingService;
    }
}
""",
    "PricingService.java": """\
package com.example.order;

import org.springframework.stereotype.Service;

@Service
public class PricingService {
    private final OrderService orderService;

    // Deliberate cycle: OrderService -> PricingService -> OrderService.
    public PricingService(OrderService orderService) {
        this.orderService = orderService;
    }
}
""",
    "OrderRepository.java": """\
package com.example.order;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.stereotype.Repository;

@Repository
public interface OrderRepository extends JpaRepository<OrderEntity, Long> {
}
""",
    "OrderEntity.java": """\
package com.example.order;

import javax.persistence.Entity;
import javax.persistence.Id;

@Entity
public class OrderEntity {
    @Id
    private Long id;
}
""",
}


def main() -> None:
    PACKAGE_DIR.mkdir(parents=True, exist_ok=True)
    (ROOT / "pom.xml").write_text(FILES["pom.xml"], encoding="utf-8")
    for name, content in FILES.items():
        if name == "pom.xml":
            continue
        (PACKAGE_DIR / name).write_text(content, encoding="utf-8")
    print(f"Wrote {len(FILES)} files under {ROOT}/")


if __name__ == "__main__":
    main()
