import com.oltpbenchmark.WorkloadConfiguration;
import com.oltpbenchmark.api.Procedure;
import com.oltpbenchmark.benchmarks.tpch.procedures.GenericQuery;
import com.oltpbenchmark.api.StatementDialects;
import com.oltpbenchmark.types.DatabaseType;
import com.oltpbenchmark.util.RandomGenerator;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.Statement;
import java.lang.reflect.Method;
import java.util.Random;

public final class TpchSingleQueryRunner {
    private TpchSingleQueryRunner() {
    }

    public static void main(String[] args) throws Exception {
        if (args.length != 6 && args.length != 7) {
            System.err.println("usage: TpchSingleQueryRunner <url> <user> <password> <query_id> <scale_factor> <work_mem> [application_name]");
            System.exit(2);
        }

        String url = args[0];
        String user = args[1];
        String password = args[2];
        int queryId = Integer.parseInt(args[3]);
        double scaleFactor = Double.parseDouble(args[4]);
        String workMem = args[5];
        String applicationName = args.length == 7 ? args[6] : "tpch_ap";

        Class.forName("org.postgresql.Driver");
        try (Connection conn = DriverManager.getConnection(url, user, password);
             Statement setup = conn.createStatement()) {
            setup.execute("SET application_name = '" + applicationName.replace("'", "''") + "'");
            setup.execute("SET work_mem = '" + workMem.replace("'", "''") + "'");
            setup.execute("SET enable_hashjoin = on");
            setup.execute("SET enable_mergejoin = on");
            setup.execute("SET enable_nestloop = on");

            Random random = new Random();
            random.setSeed(15721L);
            RandomGenerator generator = new RandomGenerator(random.nextInt());
            GenericQuery query = newQuery(queryId);
            initializeQuery(query);
            long startNs = System.nanoTime();
            System.out.printf("TPCH_Q%d_START%n", queryId);
            query.run(conn, generator, scaleFactor);
            double elapsed = (System.nanoTime() - startNs) / 1_000_000_000.0;
            System.out.printf("TPCH_Q%d_END elapsed_seconds=%.3f%n", queryId, elapsed);
        }
    }

    private static GenericQuery newQuery(int queryId) throws Exception {
        String className = "com.oltpbenchmark.benchmarks.tpch.procedures.Q" + queryId;
        Class<?> cls = Class.forName(className);
        return (GenericQuery) cls.getDeclaredConstructor().newInstance();
    }

    private static void initializeQuery(GenericQuery query) throws Exception {
        WorkloadConfiguration config = new WorkloadConfiguration();
        config.setBenchmarkName("tpch");
        config.setDatabaseType(DatabaseType.POSTGRES);
        StatementDialects dialects = new StatementDialects(config);

        Method initialize = Procedure.class.getDeclaredMethod("initialize", DatabaseType.class);
        initialize.setAccessible(true);
        initialize.invoke(query, DatabaseType.POSTGRES);

        Method loadSQLDialect = Procedure.class.getDeclaredMethod("loadSQLDialect", StatementDialects.class);
        loadSQLDialect.setAccessible(true);
        loadSQLDialect.invoke(query, dialects);
    }
}
